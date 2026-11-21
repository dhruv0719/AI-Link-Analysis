# Submission checklist (deadline: 27 Nov 2026)

Things only you can do, in order. Time estimate: ~45-60 min.

## 1. Run it with a real LLM key (15 min)
- [ ] `python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt`
- [ ] `pytest -q` → all green
- [ ] `cp .env.example .env`, set `LLM_PROVIDER` + one API key (Gemini has a free tier if you don't want to pay)
- [ ] `uvicorn app.main:app --host 0.0.0.0 --port 4000`, open `http://localhost:4000/docs`
- [ ] Analyse 2-3 real sites (include one small local business). Check the output makes sense for each site.
- [ ] Save screenshots of `/docs` and of one real response into `docs/screenshots/`; paste a real response into `docs/sample_response.json` and delete the "Provenance of the sample" note in the README.

## 2. Benchmark (10 min, optional but strongly recommended: the brief says *"do not claim maximum accuracy without testing"*)
- [ ] `python -m scripts.benchmark --models openai:gpt-6-luna anthropic:claude-haiku-5-5 gemini:gemini-3.5-flash-lite`
- [ ] Paste the printed table into README section **E. Measured results** and fix the `[not yet measured]` tags you replaced.
- [ ] If the data disagrees with the recommendation in section D, change the recommendation and the default in `.env.example` / `app/config.py` (`DEFAULT_MODELS`).
- [ ] Re-check prices on the vendor pages (they change monthly) and update `MODEL_PRICING` + the README date.

## 3. GitHub (5 min)
- [ ] `git init && git add . && git commit -m "AI link analysis API"`
- [ ] Create a **public** repo, push.
- [ ] `git grep -n -i -E "sk-|api[_-]?key *= *['\"][A-Za-z0-9]" -- . ':!tests' ':!README.md' ':!.env.example'` → nothing. Confirm `.env` is not in the repo.

## 4. Deploy (10 min)
- [ ] Render → New → Blueprint → pick the repo; add your API key in the dashboard (never in git).
- [ ] Open `https://<service>.onrender.com/health` and `/docs`; run one real analysis through Swagger.
- [ ] Put the live URL at the top of the README and commit.

## 5. Verification steps required by the brief
- [ ] Follow **Dxora Technologies** on LinkedIn: https://www.linkedin.com/company/dxora-technologies
- [ ] Follow **CREST** on Instagram: https://www.instagram.com/crestspot/
- [ ] Have your LinkedIn and Instagram profile links ready to include in the email.

## 6. Email
**To:** hr@chivoxai.com
**Subject:** `<Your Name> - Python Practical Link Analysis test review`

```text
Hello,

Please find my submission for the Python Practical Link Analysis test.

1. Full Name: <...>
2. Demo Link: https://<service>.onrender.com   (API docs: https://<service>.onrender.com/docs)
3. GitHub Repository (public): https://github.com/<you>/<repo>
4. Contact Number: <...>
5. Current Location: <...>
6. Preferred Date and Time for Final Discussion: <...>

LinkedIn: <your profile>
Instagram: <your profile>

The README contains setup steps, sample requests/responses, the LLM comparison and my model recommendation.

Thank you,
<Your Name>
```

## Be ready to explain in the panel interview
- Why one LLM call for all five sections, and why validation + repair + fallback instead of trusting JSON mode.
- The SSRF design (resolve → check every IP → manual redirects → re-check each hop) and its DNS-rebinding limitation.
- Why competitors are labelled `ai_suggested` and how `verification` works (and why the domain name itself is not evidence).
- Cost: where the tokens go, what the cache and `reasoning_effort` change, and what your *measured* benchmark numbers were.
- Known gaps: JS-rendered sites, no real competitor research API, per-process cache/rate limit.
