# Benchmark Integrity & Live Evaluation Report (Phase 3.4.1)

**Timestamp:** 2026-11-22 19:26:49 UTC  
**LLM Provider Tested:** Groq (`openai/gpt-oss-120b`)  
**TPM Limit Configured:** 8,000 Tokens/Minute  

---

## 1. Operational Reliability Metrics

Operational metrics measure infrastructure, network transport, rate limits, and provider payload boundaries across all requests.

| Total Attempts | Successful Analyses | Website Access Failures (403) | Rate Limits (429) | Payload Limits (413) | Timeouts (504) | Other Failures | Success Rate |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 12 | 8 | 2 | 2 | 0 | 0 | 0 | **66.7%** |

### Operational Accounting Methodology
- **Operational Attempt Definition:** An operational attempt is defined as one individual invocation of the analysis pipeline against a specific target URL. Internal HTTPX connection backoffs, DNS pinning retries, or LLM JSON repairs that occur within a pipeline execution do not increment the attempt counter. However, if a primary target URL fails (e.g. HTTP 403 WAF block) and an alternative fallback target URL is executed, each target URL constitutes a distinct operational attempt.
- **Counting Reconciliation:**
  - Total Operational Attempts: **12**
  - Successful Analyses: **8**
  - Recorded Operational Failures: **4** (2 Website Access Blocks [ro.co, fastenal.com], 2 Provider Rate Limits [singlegrain.com attempts 1 & 2])
  - Operational Success Rate: 8 / 12 = **66.7%**

### Detailed Operational Failure Log

| Category | Target URL | Failure Type | Error Code | Message |
|---|---|---|---|---|
| Healthcare | `https://ro.co` | **website_access_blocked (403)** | `website_access_denied` | The website refused automated access (HTTP 403). It may use bot protection or require login. |
| Digital Marketing | `https://www.singlegrain.com` | **provider_rate_limit (429)** | `llm_rate_limited` | The LLM provider is rate limiting requests. Please retry shortly. |
| Industrial B2B | `https://www.fastenal.com` | **website_access_blocked (403)** | `website_access_denied` | The website refused automated access (HTTP 403). It may use bot protection or require login. |
| Digital Marketing | `https://www.singlegrain.com` | **provider_rate_limit (429)** | `llm_rate_limited` | The LLM provider is rate limiting requests. Please retry shortly. |

---

## 2. Analysis Quality Scores (Successful Analyses Only)

> [!IMPORTANT]
> **Honest Score Interpretation — Automated Structural Evaluation vs Factual Truth:**
> - The scores below represent **automated structural evaluation metrics**, measuring schema conformity, citation existence, deterministic provenance labeling, and keyword containment.
> - They do **NOT** represent semantic business truth, subjective commercial quality, or real-world factual accuracy, which require independent human expert review.
> - Quality metrics evaluate **only successfully completed analyses** ($n \ge 1$). Infrastructure failures (e.g. HTTP 403 WAF blocks or provider 429 rate limits) are tracked strictly under **Operational Metrics** and are **never converted into 0% quality scores**.
> - When evaluation data is unavailable (e.g. absent raw page corpus), dimensions report **N/A** rather than awarding unearned scores.

### Evaluation Dimensions & Weights
Each of the six evaluation dimensions carries an equal weight ($1/6$):
1. **Dimension A: Website Understanding ($1/6$)**: Brand name identification, category identification, business summary, target audience, ground-truth brand match.
2. **Dimension B: Evidence Grounding & Provenance ($1/6$)**: Verbatim quote presence in crawled page corpus via collision-safe URL and redirect-alias matching, and explicit provenance tagging (`website` vs `inferred`). (Requires raw fetched page corpus; reports `N/A` if absent).
3. **Dimension C: Competitor Intelligence ($1/6$)**: Competitor relevance assessment, comparison table consistency, and disqualification of invalid competitors.
4. **Dimension D: Search Query & Platform Coverage ($1/6$)**: Exactly 5 queries, coverage of all 5 platforms (Google, LinkedIn, X, Facebook, Instagram), valid intent categorization.
5. **Dimension E: SEO Improvements ($1/6$)**: Recommended title length (10-70 chars), meta description length (70-160 chars), on-page audit items.
6. **Dimension F: Services & Target Audience ($1/6$)**: Top services identified (1-5), primary audience identified, needs and marketing segments provided.

**Score Formula:**
$$\text{structural\_score} = \frac{1}{|D_{\text{evaluable}}|} \sum_{d \in D_{\text{evaluable}}} S(d)$$

### Ground-Truth Scoring Audit
Of the fields defined in `benchmarks/ground_truth.json`:
- **Evaluated in Automated Structural Score:**
  - `expected_brand_names` (Dimension A - brand identification match)
  - `known_invalid_competitors` (Dimension C - competitor disqualification check)
- **Stored as Reference Metadata for Human Expert Review (NOT automatically scored):**
  - `expected_categories`
  - `expected_core_services`
  - `prohibited_fabricated_services`
  - `known_valid_competitors`
  - `expected_geographic_focus`
  - `location_stated_on_site`
- **Human Semantic Review Checklist:** Items from `human_review_criteria` are surfaced explicitly below in `needs_manual_review` for human expert evaluation.

| Category | Target URL | Successful Runs (n) | Understanding | Grounding | Competitors | Search Queries | SEO | Services & Audience | Structural Score | Needs Manual Review |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| **SaaS** | `https://plausible.io/` | 1 | 100% | 100% | 100% | 100% | 100% | 100% | **100%** | 2 item(s) |
| **E-commerce** | `https://www.allbirds.com/` | 1 | 100% | 100% | 100% | 100% | 33% | 100% | **89%** | 2 item(s) |
| **Digital Marketing** | `https://www.singlegrain.com/` | 1 | 100% | 83% | 100% | 100% | 33% | 100% | **86%** | 2 item(s) |
| **Healthcare** | `https://www.teladochealth.com/` | 1 | 100% | 100% | 100% | 100% | 100% | 100% | **100%** | 2 item(s) |
| **EdTech** | `https://www.coursera.org/` | 1 | 100% | 100% | 100% | 100% | 67% | 100% | **94%** | 2 item(s) |
| **Local Services** | `https://www.rotorooter.com/` | 1 | 100% | 100% | 100% | 100% | 33% | 100% | **89%** | 2 item(s) |
| **Developer Platform** | `https://github.com/` | 1 | 100% | 100% | 100% | 100% | 100% | 100% | **100%** | 2 item(s) |
| **Industrial B2B** | `https://www.emachineshop.com/` | 1 | 100% | 100% | 100% | 100% | 67% | 100% | **94%** | 2 item(s) |

**Mean Structural Score on Successful Analyses:** `94.1%` (across 8 successful categories)

> [!NOTE]
> **Arithmetic Score Reconciliation:**
> The exact arithmetic mean of unrounded category scores is $\frac{1.0000 + 0.8889 + 0.8611 + 1.0000 + 0.9444 + 0.8889 + 1.0000 + 0.9444}{8} = \frac{7.5278}{8} = 94.097\%$ (displays as `94.1%` unrounded, or `94.0%` if summing integer rounded table percentages $\frac{752}{8} = 94.0\%$).

### Items Requiring Human Judgment (`needs_manual_review`)

#### SaaS (`https://plausible.io/`)
- [ ] Verify whether competitor differentiation accurately captures cookie-free and GDPR positioning.
- [ ] Review whether recommended marketing segments target privacy-conscious operators rather than generic enterprise.

#### Healthcare (`https://www.teladochealth.com/`)
- [ ] Check whether medical clinical care is separated from consumer wellness or pharmaceutical manufacturing.
- [ ] Verify that health search queries avoid making unsubstantiated clinical efficacy claims.

#### EdTech (`https://www.coursera.org/`)
- [ ] Verify whether university partnership credentials and certificates are distinguished from generic video tutorials.
- [ ] Assess whether B2B enterprise training is separated from consumer learner segments.

#### Industrial B2B (`https://www.emachineshop.com/`)
- [ ] Review whether precision tolerances and custom fabrication options are accurately distinguished from off-the-shelf fastener distribution.
- [ ] Check whether LinkedIn queries focus on engineering procurement and manufacturing sourcing.

#### Digital Marketing (`https://www.singlegrain.com/`)
- [ ] Review whether search queries target prospective enterprise clients searching for growth agencies.
- [ ] Verify that Google and Meta are not misidentified as direct agency competitors.

#### E-commerce (`https://www.allbirds.com/`)
- [ ] Review whether product materials (merino wool, eucalyptus fiber, sugarcane) are appropriately reflected.
- [ ] Check whether search queries target eco-friendly and everyday comfort footwear shoppers.

#### Local Services (`https://www.rotorooter.com/`)
- [ ] Confirm that emergency 24/7 service and drain specialization are prominently highlighted.
- [ ] Check whether Instagram and Facebook search queries properly utilize local community recommendation phrasing.

#### Developer Platform (`https://github.com/`)
- [ ] Examine whether enterprise security and CI/CD capabilities are recognized alongside core open-source hosting.
- [ ] Verify that developer-focused search queries avoid non-technical jargon.

---

## 3. Offline Fixture Consistency Validation

> [!NOTE]
> **Methodological Limitation Notice:**
> The offline benchmark validates schema conformity, quotation extraction invariants, and evaluator consistency against prepared test fixtures (`benchmarks/categories_fixtures.json`).
> **It does NOT establish real-world model accuracy.** Real-world model quality is measured strictly in Section 2.

| Category | Fixture URL | Understanding | Grounding | Competitors | Search Queries | SEO | Services & Audience | Structural Score (5 Dim) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **SaaS** | `https://plausible.io` | 100% | N/A | 100% | 100% | 100% | 100% | **100%** |
| **E-commerce** | `https://www.allbirds.com` | 100% | N/A | 100% | 100% | 100% | 100% | **100%** |
| **Digital marketing / agency** | `https://jayjagleinnovations.com` | 100% | N/A | 100% | 100% | 100% | 100% | **100%** |
| **Healthcare** | `https://www.mayoclinic.org` | 90% | N/A | 100% | 100% | 100% | 100% | **98%** |
| **EdTech** | `https://www.coursera.org` | 100% | N/A | 100% | 100% | 100% | 100% | **100%** |
| **Local services / trades** | `https://www.rotorooter.com` | 100% | N/A | 100% | 100% | 100% | 100% | **100%** |
| **Developer platform** | `https://github.com` | 100% | N/A | 100% | 100% | 100% | 100% | **100%** |
| **Industrial B2B** | `https://www.emachineshop.com` | 100% | N/A | 100% | 100% | 100% | 100% | **100%** |

---

## 4. How to Reproduce

```bash
# 1. Run offline fixture consistency validation:
python -m scripts.run_benchmark_8_categories --offline

# 2. Run live evaluation for a single category (e.g. SaaS):
python -m scripts.run_benchmark_8_categories --live --category SaaS

# 3. Run live evaluation for selected categories with 30s delay:
python -m scripts.run_benchmark_8_categories --live --categories SaaS "Digital Marketing" Developer Platform --delay 30

# 4. Resume interrupted live benchmark without repeating completed categories:
python -m scripts.run_benchmark_8_categories --live --resume --delay 30
```