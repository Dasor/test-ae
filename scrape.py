#!/usr/bin/env python3
"""Scrape questions from saved Sakai (Samigo) exam review pages into questions.json.

Usage: python scrape.py [html files or dirs...]   (default: exams/*.html)

Re-running is safe: questions already in questions.json are kept, new ones are
added, and duplicates (same text + same options, in any order) are merged.

Each question's topic is the name of the exam part it appears in. topics.json
(optional) renames topics. Keys can be a part name ("test 1"), an exam title
("Test 5") or both ("Test 5 / Ridge, Lasso, Selection"); the most specific wins.
"""
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "questions.json"
TOPICS_FILE = ROOT / "topics.json"
TOPIC_NAMES = json.loads(TOPICS_FILE.read_text(encoding="utf-8")) if TOPICS_FILE.exists() else {}


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def norm(text):
    """Key used for duplicate detection: case, accents and whitespace insensitive."""
    text = unicodedata.normalize("NFKD", clean(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def question_id(text, options):
    key = norm(text) + "||" + "|".join(sorted(norm(o) for o in options))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def restore_math(root):
    """Swap MathJax-rendered markup for its original LaTeX source, delimited for KaTeX."""
    for el in root.select(".MathJax_Preview, .MathJax, .MathJax_Display"):
        el.decompose()
    for script in root.find_all("script", type=re.compile(r"^math/tex")):
        tex = script.get_text().strip()
        display = "mode=display" in script["type"]
        script.replace_with(f"\\[{tex}\\]" if display else f"\\({tex}\\)")


def stem_text(fieldset):
    """Question text = everything in the fieldset before the first table/options block."""
    parts = []
    for node in fieldset.children:
        if getattr(node, "name", None) in ("table", "div", "ul", "script"):
            break
        parts.append(str(node) if isinstance(node, NavigableString) else node.get_text(" "))
    return clean(" ".join(parts))


def parse_question(callout):
    fieldset = callout.find("fieldset") or callout  # true/false questions have no fieldset
    restore_math(fieldset)
    for s in fieldset.find_all("script"):
        s.decompose()

    key_el = fieldset.find(id=re.compile(r":answerKey[A-Za-z]*$"))
    if key_el is None:
        return None
    key = clean(key_el.get_text())

    text = stem_text(fieldset)
    labels, options = [], []
    for li in fieldset.select("li.samigo-question-answer"):
        lab = li.select_one(".samigo-answer-label")
        txt = li.select_one(".samigo-answer-text")
        if txt is not None:  # multiple choice
            labels.append(clean(lab.get_text()).rstrip(".") if lab else "")
            options.append(clean(txt.get_text()))
    for label in fieldset.select("table.tableTF label"):  # true / false
        opt = clean(label.get_text(" "))
        labels.append(opt)
        options.append(opt)

    if not text or not options:
        return None

    # The key is a letter ("B") or comma-separated letters for MC, or the option text for T/F.
    wanted = [k.strip().rstrip(".") for k in key.split(",")]
    answers = [o for l, o in zip(labels, options) if l in wanted or norm(o) in map(norm, wanted)]
    if not answers:
        print(f"  ! could not resolve answer {key!r} for: {text[:60]}", file=sys.stderr)
        return None

    return {
        "id": question_id(text, options),
        "type": "truefalse" if labels == options else "multiple",
        "question": text,
        "options": options,
        "answers": answers,
    }


def scrape(path):
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    title = soup.find("input", id="takeAssessmentForm:assessTitle")
    exam = clean(title["value"]) if title else path.stem
    for callout in soup.select("div.samigo-question-callout"):
        q = parse_question(callout)
        if q:
            # Topic = name of the exam part the question belongs to ("Parte 1 de 2 - <name>").
            header = callout.find_previous("h4", class_="part-header")
            topic = exam
            if header:
                for badge in header.select(".badge"):
                    badge.decompose()
                topic = re.sub(r"^Parte \d+ de \d+\s*-\s*", "", clean(header.get_text())) or exam
            q["exams"] = [exam]
            q["topics"] = [topic_name(exam, topic)]
            yield q


def topic_name(exam, part):
    """Most specific rename in topics.json wins: "<exam> / <part>", then "<exam>", then "<part>"."""
    for key in (f"{exam} / {part}", exam, part):
        if key in TOPIC_NAMES:
            return TOPIC_NAMES[key]
    return part


def main(args):
    paths = []
    for a in args or [ROOT / "exams"]:
        p = Path(a)
        paths += sorted(p.glob("*.html")) if p.is_dir() else [p]

    db = {}
    if OUT.exists():
        db = {q["id"]: q for q in json.loads(OUT.read_text(encoding="utf-8"))}
    before = len(db)
    seen = set()  # questions found in this run get their sources/exams/topics rebuilt from scratch

    for path in paths:
        found = new = 0
        for q in scrape(path):
            found += 1
            existing = db.get(q["id"])
            if existing is None or q["id"] not in seen:
                new += existing is None
                q["sources"] = [path.name]
                db[q["id"]] = q
                seen.add(q["id"])
            else:
                if existing["answers"] != q["answers"]:
                    print(f"  ! answer mismatch for {q['id']}: {existing['answers']} vs {q['answers']}",
                          file=sys.stderr)
                for field, values in (("sources", [path.name]), ("exams", q["exams"]), ("topics", q["topics"])):
                    existing.setdefault(field, [])
                    existing[field] += [v for v in values if v not in existing[field]]
        print(f"{path.name}: {found} questions, {new} new")

    OUT.write_text(json.dumps(list(db.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{OUT.name}: {len(db)} unique questions ({len(db) - before} added)")


if __name__ == "__main__":
    main(sys.argv[1:])
