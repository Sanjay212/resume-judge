"""JUDGE: a resume-vs-job-posting scorer.

Every resume version is scored the same way, so scores can be compared:

1. Items: the posting is split into scoreable items by fixed rules (extract_items).
2. Links: a model links each item to resume bullets as strong / partial / weak,
   with a short quoted reason. Versions are shown blind (A, B, ...) in one call,
   so the same standard is applied to all of them (judge_links).
3. Score: match % = (strong x 1 + partial x 0.5 + weak x 0.25) / items, using the
   best link per item; plus strong count, gaps and unused bullets (score).
4. Keywords: which posting keywords appear in bullets, only in skills, or nowhere.
5. Rules: hard resume rules (repeated numbers, reverse chronological order,
   word budget, duplicate bullets, skills with no bullet behind them, keyword echo).

Only step 2 calls the Claude API. Everything else runs offline, and
`--links` re-scores saved links without any API call.

Usage:
  python judge.py --posting job.json --resume before=a.json --resume after=b.json --out report.json
  python judge.py --posting job.json --resume before=a.json --links links.json   # offline re-score

Resume JSON: {"summary": str, "jobs": [[header, [bullet, ...]], ...], "skills": str, "edu": [str]}
Header format: "Employer | Title | Start - End"  (End may be "Present").
Posting JSON: {"company", "job_title", "sections": {"2_requirements": [...], "3_job_responsibilities": [...],
  "4_preferred_qualifications": [...], "5_about_the_job": str, "8_keywords": {section: {keyword: count}}}}
  (the layout of a Teal job-tracker export; see examples/posting.json).
"""
from __future__ import annotations

import argparse
import difflib
import json
import random
import re
import sys
from collections import Counter, defaultdict

RUBRIC_VERSION = "JUDGE v1 (2026-10-21)"
RULES_VERSION = ("rules v1.3 (2026-10-23): every number is detected, small counts included; only numbers marked "
                 "single-use may not repeat")
MODEL = "claude-opus-5-5"
STRENGTH_POINTS = {"s": 1.0, "p": 0.5, "w": 0.25}
MAX_LINKS_PER_ITEM = 4
MAX_WHY_WORDS = 20
WORD_BUDGET = 700

RUBRIC = """You are a strict, impartial recruiter scoring how well resumes support a job posting.

For EACH resume, link every posting item to the bullets that give evidence for it.
- Up to 4 links per item. An item may have 0 links if nothing supports it.
- Strength: "s" = the bullet directly shows the candidate did this; "p" = partial or
  adjacent evidence; "w" = weak, only implied.
- Judge only bullet text, plus employer names and dates in headers for
  years-of-experience items. Education lines can support degree items. Ignore
  summaries and skills lists.
- Keyword echo is not evidence: a bullet that repeats the posting's words without
  showing the work is "w" at most.
- A candidate's own years as a working scientist count as direct knowledge of
  scientists' workflows.
- Items that describe the company's own products ask: has the candidate built
  something comparable? Judge on that.
- Apply exactly the same standard to every resume. You do not know which version
  is newer, and it does not matter.
- For each link, give a "why" of 20 words or fewer that quotes the key bullet phrase.
Use only the item ids and bullet ids given."""

BOILERPLATE = re.compile(
    r"nobody checks every box|please apply|don.t think you meet|equal opportunity|"
    r"we encourage|reasonable accommodation", re.I)
SKIP_ABOUT = re.compile(r"\bcontract\b|\bremote\b|\bopportunity with\b|\bsalary\b|\bbenefits package\b", re.I)


# ---------------------------------------------------------------- 1. items
def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text or "")
    return [p.strip() for p in parts if len(p.split()) >= 6]


def extract_items(posting: dict) -> list[dict]:
    """Return [{"id": "j{s}_{i}", "section": name, "text": str}] by fixed rules.

    Sections, in order: Requirements (2_), Job responsibilities (3_), then, only when
    one of those two is missing, the About-the-job sentences as "Role duties (from
    About the job)" (contract, remote and pitch sentences skipped), then Preferred
    (4_, boilerplate dropped).
    """
    s = posting.get("sections", posting)
    sections = []
    req = [x for x in s.get("2_requirements") or [] if x.strip()]
    resp = [x for x in s.get("3_job_responsibilities") or [] if x.strip()]
    pref = [x for x in s.get("4_preferred_qualifications") or [] if x.strip() and not BOILERPLATE.search(x)]
    if req:
        sections.append(("Requirements", req))
    if resp:
        sections.append(("Job responsibilities", resp))
    if not req or not resp:
        about = [x for x in _sentences(s.get("5_about_the_job", "")) if not SKIP_ABOUT.search(x)]
        if about:
            sections.append(("Role duties (from About the job)", about))
    if pref:
        sections.append(("Preferred", pref))
    return [{"id": f"j{si}_{i}", "section": name, "text": t}
            for si, (name, items) in enumerate(sections) for i, t in enumerate(items)]


def items_fingerprint(items: list[dict]) -> str:
    """Short hash of the item list; saved links are only valid for the same items."""
    import hashlib
    return hashlib.sha256(json.dumps([[i["id"], i["text"]] for i in items]).encode()).hexdigest()[:12]


def bullet_ids(resume: dict) -> dict[str, str]:
    return {f"c{a}_{i}": b for a, (h, bs) in enumerate(resume["jobs"]) for i, b in enumerate(bs)}


def employer(header: str) -> str:
    return header.split("|")[0].strip()


# ---------------------------------------------------------------- 2. links (model)
def _schema(labels: list[str]) -> dict:
    link = {"type": "object", "additionalProperties": False,
            "required": ["j", "c", "s", "why"],
            "properties": {"j": {"type": "string"}, "c": {"type": "string"},
                           "s": {"type": "string", "enum": ["s", "p", "w"]},
                           "why": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": labels,
            "properties": {L: {"type": "array", "items": link} for L in labels}}


def judge_links(posting: dict, resumes: dict[str, dict], seed: int = 7,
                model: str = MODEL, effort: str = "high") -> dict[str, list[dict]]:
    """Blind-judge all resume versions in one call. Returns {version_name: links}."""
    import anthropic  # imported here so offline use needs no SDK

    items = extract_items(posting)
    names = list(resumes)
    rng = random.Random(seed)
    order = names[:]
    rng.shuffle(order)
    labels = [chr(65 + i) for i in range(len(order))]
    blind = dict(zip(labels, order))
    payload = {"posting": {"company": posting.get("company", ""), "title": posting.get("job_title", ""),
                           "items": {it["id"]: it["text"] for it in items}}}
    for L, name in blind.items():
        r = resumes[name]
        payload["resume_" + L] = {
            "bullets": {cid: f"{employer(r['jobs'][int(cid[1:].split('_')[0])][0])}: {b}"
                        for cid, b in bullet_ids(r).items()},
            "headers": [h for h, _ in r["jobs"]], "education": r.get("edu", [])}

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=model, max_tokens=64000,
        system=RUBRIC,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": _schema(labels)}},
        messages=[{"role": "user", "content": "Score these resumes. Return links for "
                   + ", ".join(labels) + ".\n\n" + json.dumps(payload, indent=1)}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Judge declined: {getattr(msg, 'stop_details', None)}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Judge output hit max_tokens; links incomplete.")
    text = next(b.text for b in msg.content if b.type == "text")
    raw = json.loads(text)
    out = {blind[L]: validate_links(raw.get(L, []), items, resumes[blind[L]]) for L in labels}
    out["_items"] = items_fingerprint(items)
    out["_rubric"] = RUBRIC_VERSION
    return out


def validate_links(links: list[dict], items: list[dict], resume: dict) -> list[dict]:
    """Drop bad ids and duplicates, keep the 4 strongest links per item, trim reasons."""
    J = {it["id"] for it in items}
    C = set(bullet_ids(resume))
    rank = {"s": 0, "p": 1, "w": 2}
    seen, per = set(), defaultdict(list)
    for l in links:
        key = (l.get("j"), l.get("c"))
        if l.get("j") in J and l.get("c") in C and l.get("s") in rank and key not in seen:
            seen.add(key)
            why = " ".join(str(l.get("why", "")).split()[:MAX_WHY_WORDS])
            per[l["j"]].append({"j": l["j"], "c": l["c"], "s": l["s"], "why": why})
    out = []
    for j in sorted(per, key=lambda x: [int(n) for n in x[1:].split("_")]):
        out += sorted(per[j], key=lambda l: rank[l["s"]])[:MAX_LINKS_PER_ITEM]
    return out


# ---------------------------------------------------------------- 3. score
def score(items: list[dict], links: list[dict], resume: dict) -> dict:
    best = {}
    for l in links:
        p = STRENGTH_POINTS[l["s"]]
        best[l["j"]] = max(best.get(l["j"], 0), p)
    n = len(items) or 1
    used = {l["c"] for l in links}
    return {
        "match_pct": round(100 * sum(best.get(it["id"], 0) for it in items) / n),
        "items": len(items),
        "strong": sum(1 for it in items if best.get(it["id"]) == 1.0),
        "partial": sum(1 for it in items if best.get(it["id"]) == 0.5),
        "weak": sum(1 for it in items if best.get(it["id"]) == 0.25),
        "gaps": [it["id"] for it in items if it["id"] not in best],
        "unused_bullets": [c for c in bullet_ids(resume) if c not in used],
    }


# ---------------------------------------------------------------- 4. keywords
def _stem_rx(kw: str) -> re.Pattern:
    words = [re.escape(w.lower().rstrip("s")) for w in re.split(r"[\s\-]+", kw) if w]
    body = r"[\s\-]+".join(w + r"(?:s|es|ed|d|ing)?" for w in words)
    return re.compile(r"\b" + body + r"\b", re.I)


def keywords(posting: dict, resume: dict) -> dict:
    kw = (posting.get("sections", posting).get("8_keywords") or {})
    terms = sorted({k for group in kw.values() for k in group})
    bullets = " ".join([resume.get("summary", "")] + [b for _, bs in resume["jobs"] for b in bs])
    skills = resume.get("skills", "")
    out = {"placed": [], "skills_only": [], "missing": []}
    for t in terms:
        rx = _stem_rx(t)
        out["placed" if rx.search(bullets) else "skills_only" if rx.search(skills) else "missing"].append(t)
    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    return out


# ---------------------------------------------------------------- 5. rules
NUM = re.compile(r"(?<![\w$])\$?~?\d[\d,]*(?:\.\d+)?\s?(?:%|x\b|[MBK]\b|\+)?")
YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _numbers(text: str) -> list[str]:
    """Every number on the resume except years: %, x, $, K/M/B, +, and bare counts of any size.

    Years are dates, not metrics. Rules v1.2 (2026-10-22, "number catch"): bare counts under 10
    count too, so "2 months" in the summary and a bullet, or "3 jurisdictions" and "3 parallel trains",
    is a repeated number. (v1.1 had let small counts repeat, and the summary repeats slipped through.)
    """
    out = []
    for m in NUM.finditer(text):
        n = m.group(0).strip().replace("~", "").rstrip(",.")
        if YEAR.fullmatch(n.strip("$")):
            continue
        out.append(n)
    return out


def _end_year(header: str) -> int:
    tail = header.split("|")[-1]
    if re.search(r"present|current", tail, re.I):
        return 9999
    ys = [int(y) for y in re.findall(r"(?:19|20)\d{2}", tail)]
    return max(ys) if ys else -1


def rules(resume: dict, posting: dict | None = None, single_use=None) -> dict:
    """Hard resume rules. `single_use`: numbers that may appear only once on the page (for example a headline
    metric that should not be echoed between the summary and a bullet). Rules v1.3 (2026-10-23): `repeated_numbers`
    lists every repeat for information; `repeated_single_use` is what fails `passes`. Without `single_use`, no
    repeat fails."""
    bullets = [b for _, bs in resume["jobs"] for b in bs]
    body = " ".join([resume.get("summary", "")] + bullets)
    words = len(" ".join([body, resume.get("skills", ""), " ".join(resume.get("edu", []))]).split())
    nums = Counter(_numbers(body))
    # research and teaching blocks are ordered on their own, after experience
    exp = [(h, _end_year(h)) for h, _ in resume["jobs"] if not re.match(r"\s*(RESEARCH|TEACHING)\b", h)]
    order = [f"{employer(a)} ({ya}) listed before {employer(b)} ({yb})"
             for (a, ya), (b, yb) in zip(exp, exp[1:]) if yb > ya and ya != -1]
    dupes = []
    norm = [re.sub(r"\W+", " ", b.lower()).strip() for b in bullets]
    for i in range(len(norm)):
        for k in range(i + 1, len(norm)):
            if difflib.SequenceMatcher(None, norm[i], norm[k]).ratio() > 0.8:
                dupes.append([bullets[i], bullets[k]])
    bullet_text = " ".join(bullets)
    skills = [s.strip() for s in re.split(r"[;,]", resume.get("skills", "")) if s.strip()]
    unbacked = [s for s in skills if not any(_stem_rx(w).search(bullet_text)
                                             for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", s))]
    echo = []
    if posting:
        kw = posting.get("sections", posting).get("8_keywords") or {}
        terms = {k.lower() for g in kw.values() for k in g if len(k) > 3}
        for b in bullets:
            m = re.search(r"\b(?:in|for|as|with|to stay|through)\s+([\w\s\-/&]+?)[.,]?\s*$", b)
            if m and m.group(1).strip().lower() in terms:
                echo.append(b)
            elif re.search(r",\s*(hands-on|for a track record)\.?$", b, re.I):
                echo.append(b)
    repeats = {n: c for n, c in nums.items() if c > 1}
    est = {str(e).replace(" ", "").replace(",", "").lower() for e in (single_use or ())}
    single_repeats = {n: c for n, c in repeats.items() if n.replace(" ", "").replace(",", "").lower() in est}
    return {
        "word_count": words, "over_budget": words > WORD_BUDGET,
        "repeated_numbers": repeats,
        "repeated_single_use": single_repeats,
        "out_of_order": order,
        "duplicate_bullets": dupes,
        "skills_without_bullet": unbacked,
        "keyword_echo": echo,
        "passes": words <= WORD_BUDGET and not order and not dupes and not single_repeats and not echo,
    }


# ---------------------------------------------------------------- report
def report(posting: dict, resumes: dict[str, dict], links: dict[str, list[dict]]) -> dict:
    items = extract_items(posting)
    fp = links.get("_items")
    if fp is None:
        print("warning: links have no item fingerprint; make sure they were judged on these items",
              file=sys.stderr)
    elif fp != items_fingerprint(items):
        raise ValueError("links were judged on a different item list; re-run the judge")
    out = {"rubric": RUBRIC_VERSION, "model": MODEL, "items_fingerprint": items_fingerprint(items),
           "posting": {"company": posting.get("company"), "title": posting.get("job_title")},
           "items": items, "versions": {}}
    for name, r in resumes.items():
        if name not in links:
            raise ValueError(f"no links for version '{name}'")
        L = validate_links(links.get(name, []), items, r)
        out["versions"][name] = {"score": score(items, L, r), "keywords": keywords(posting, r),
                                 "rules": rules(r, posting), "links": L}
    return out


def summary_table(rep: dict) -> str:
    rows = [f"{rep['posting']['company']} | {rep['posting']['title']} | {rep['rubric']}",
            "version | match | strong | partial | weak | gaps | unused | keywords placed | rules"]
    for name, v in rep["versions"].items():
        s, k, r = v["score"], v["keywords"]["counts"], v["rules"]
        flags = [f"{len(r['repeated_single_use'])} repeated single-use numbers" if r["repeated_single_use"] else "",
                 f"{len(r['out_of_order'])} order breaks" if r["out_of_order"] else "",
                 f"{len(r['duplicate_bullets'])} duplicate pairs" if r["duplicate_bullets"] else "",
                 f"{len(r['keyword_echo'])} echo" if r["keyword_echo"] else "",
                 f"{r['word_count']} words" + (" (over)" if r["over_budget"] else "")]
        rows.append(f"{name} | {s['match_pct']}% | {s['strong']}/{s['items']} | {s['partial']} | {s['weak']} | "
                    f"{len(s['gaps'])} | {len(s['unused_bullets'])} | {k['placed']}/{sum(k.values())} | "
                    + ", ".join(f for f in flags if f))
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--posting", required=True, help="posting JSON (see examples/posting.json)")
    ap.add_argument("--resume", action="append", required=True, help="name=path.json (repeatable)")
    ap.add_argument("--links", help="saved links JSON {name: [links]}; skips the model call")
    ap.add_argument("--out", help="write the full report JSON here")
    ap.add_argument("--save-links", help="write the model's links here for later re-scoring")
    a = ap.parse_args(argv)
    posting = json.load(open(a.posting))
    resumes = {}
    for spec in a.resume:
        name, _, path = spec.partition("=")
        resumes[name] = json.load(open(path))
    links = json.load(open(a.links)) if a.links else judge_links(posting, resumes)
    if a.save_links:
        json.dump(links, open(a.save_links, "w"), indent=1)
    rep = report(posting, resumes, links)
    if a.out:
        json.dump(rep, open(a.out, "w"), indent=1)
    print(summary_table(rep))


if __name__ == "__main__":
    sys.exit(main())
