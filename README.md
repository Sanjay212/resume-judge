# resume-judge

A scorer that measures how well a resume supports a specific job posting, and a set of hard rules a resume must pass before it goes out.

It is one piece of **ResumeEngine**, a private system I built in October 2026 to produce tailored resumes, cover letters and 30-60-90 day plans from a corpus of verified career facts. The rest of the engine stays private because it is wired to my own career data. This repo is the part that stands on its own.

## How it was built

I built ResumeEngine by directing AI coding agents (Anthropic's Claude Code), not by typing the code myself. My job was product owner and engineering manager for a small team of agents:

- **Several agent sessions on three machines plus one in the cloud.** Each had a role: one owned the engine code and its rules, and the others ran builds, converted output and kept the job tracker current.
- **git and GitHub as the source of truth.** Every change landed as a commit on `main` in a private repo (about 30 commits from Oct 13 to Oct 26, 2026, ~10,000 lines of Python). Each machine pulled from GitHub before syncing a Drive mirror, and the agents told each other before touching shared data so they didn't push competing commits.
- **Rules came from me.** I wrote the rules in plain English (for example, "no number may repeat between the summary and the bullets"). The agents turned them into code, with tests. I reviewed the results and approved or rejected each change.

The scorer in this repo exists because I needed an honest way to tell whether a rebuilt resume was better or only stuffed with keywords.

## What the scorer does

```
posting.json ──► 1. items ──┐
                            ├─► 2. links (one blind model call) ──► 3. score ──► report
resume A, B … ──────────────┘                                        4. keywords
                                                                     5. rules
```

1. **Items.** The posting is split into scoreable items with fixed rules: requirements, then responsibilities, then preferred qualifications. The About section is used only when one of the first two is missing, and contract, remote and pitch sentences are skipped.
2. **Links.** One Claude call sees every resume version **blind** (labeled A, B and so on, in a seeded random order). It links each item to up to 4 bullets as strong, partial or weak, each with a quoted reason of 20 words or fewer. Keyword echo counts as weak at most, and summaries and skills lists are ignored.
3. **Score.** Match % = (strong × 1 + partial × 0.5 + weak × 0.25) ÷ items, using the best link per item, plus gaps and unused bullets.
4. **Keywords.** Each posting keyword is classed as placed in a bullet, only in skills, or missing. Word stems match.
5. **Rules.** A resume passes only if it has:
   - no repeat of a number marked single-use
   - jobs in reverse chronological order
   - no near-duplicate bullets
   - no keyword-stuffed tails ("…, in financial services.")
   - 700 words or fewer

   It also reports skills that no bullet backs up.

Only step 2 calls the API. Saved links carry a fingerprint of the item list, so you can re-score offline, and if the posting changes, re-scoring fails loudly instead of matching links to the wrong items.

## Try it (no API key needed)

```
python3 judge/judge.py --posting examples/posting.json \
    --resume before=examples/resume_before.json --resume after=examples/resume_after.json \
    --links examples/links.json
```

```
Example Fintech (fictional) | Senior Product Manager, Payments Operations | JUDGE v1 (2026-10-21)
version | match | strong | partial | weak | gaps | unused | keywords placed | rules
before | 31% | 0/8 | 4 | 2 | 2 | 1 | 2/14 | 59 words
after | 94% | 7/8 | 1 | 0 | 0 | 0 | 11/14 | 129 words
```

The posting and both resumes are fictional. `before` is a generic resume; `after` is the same fictional person's history, rewritten to show the work. To judge your own versions live, drop `--links`, set `ANTHROPIC_API_KEY`, and `pip install anthropic`.

Tests run offline: `python3 tests/test_judge.py`.

## Also here

`tools/todocx.py` turns the engine's resume and cover-letter HTML into Word files (python-docx for the resume, pandoc for the cover letter and its 30-60-90 plan). Recruiters often want an editable Word file.

## Formats

- **Resume:** `{"summary": str, "jobs": [["Employer | Title | Start - End", [bullet, …]], …], "skills": str, "edu": [str]}`
- **Posting:** the layout of a Teal job-tracker export. See `examples/posting.json`.
