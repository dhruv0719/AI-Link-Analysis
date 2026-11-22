"""Pydantic models: API request/response and the structure the LLM must return."""
from __future__ import annotations

from typing import Any, Literal, Optional, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Platform = Literal["Google", "Facebook", "Instagram", "LinkedIn", "X"]

_PLATFORM_ALIASES = {
    "google": "Google", "google search": "Google", "google ads": "Google", "youtube": "Google",
    "facebook": "Facebook", "fb": "Facebook", "meta": "Facebook", "facebook search": "Facebook",
    "instagram": "Instagram", "ig": "Instagram",
    "linkedin": "LinkedIn", "linked in": "LinkedIn",
    "x": "X", "twitter": "X", "x (twitter)": "X", "x/twitter": "X", "twitter/x": "X", "x (formerly twitter)": "X",
}

ServiceSource = Literal["website", "inferred"]
Provenance = Literal["website_verified", "inferred", "unverified", "recommendation"]
ContentType = Literal[
    "service_landing_page", "industry_page", "faq", "blog_topic",
    "location_page", "supporting_article", "case_study", "comparison_page", "other",
]
Verification = Literal[
    "verified_live_site", "live_site_name_not_matched", "unreachable",
    "invalid_website_url", "no_website_provided", "not_checked",
]

RelevanceStatus = Literal[
    "likely_relevant",
    "possible",
    "low_relevance",
    "category_mismatch",
    "insufficient_evidence",
    "not_checked",
]

CompetitorType = Literal[
    "direct",
    "indirect",
    "substitute",
    "unknown",
]


def _truncate_list(value: Any, limit: int) -> Any:
    return value[:limit] if isinstance(value, list) and len(value) > limit else value


# ------------------------------------------------------------------ request
class LinkAnalysisRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048, description="Website URL to analyse.")

    model_config = ConfigDict(json_schema_extra={"examples": [{"url": "https://example.com"}]})


# ------------------------------------------------------------------ LLM-facing models
class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class Evidence(_Base):
    quote: str = Field(..., min_length=1, max_length=500, description="Short supporting excerpt from the website")
    source_url: str = Field(..., min_length=1, max_length=2048, description="URL of the page where the quote appears")

    @field_validator("quote", "source_url")
    @classmethod
    def _validate_not_empty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Evidence field cannot be empty")
        return s


class Brand(_Base):
    name: str = Field(..., description="Brand / company name as used by the business")
    category: str = Field(..., description="Main business category")
    summary: str = Field(..., description="1-2 sentence summary of what the business does")
    provenance: Provenance = Field(
        "inferred",
        description="Overall brand provenance (website_verified only if both brand name and summary are verified)."
    )
    name_provenance: Provenance = Field(
        "inferred",
        description="Provenance of the brand name itself."
    )
    name_evidence: list[Evidence] = Field(
        default_factory=list,
        max_length=5,
        description="Inspectable evidence grounding the brand name from page metadata/content."
    )
    summary_provenance: Provenance = Field(
        "inferred",
        description="Provenance of the brand summary."
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        max_length=5,
        description="Evidence grounding the brand summary."
    )


class LLMCompetitor(_Base):
    name: str
    website: Optional[str] = Field(None, description="Official domain, e.g. https://competitor.com")
    description: str = ""
    target_market: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(
        "inferred",
        description="Competitor details are AI suggestions, not verified facts."
    )


class ComparisonEntry(_Base):
    brand_name: str
    role: Literal["your_brand", "competitor"]
    main_service: str = ""
    target_market: str = ""
    key_strength: str = ""
    opportunity: str = ""
    provenance: Provenance = Field(
        "inferred",
        description="Comparison rows are AI analytical interpretations."
    )

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v):
        text = str(v).strip().lower().replace(" ", "_")
        return "your_brand" if text in {"your_brand", "brand", "you", "yours"} else text


class CompetitorAnalysisLLM(_Base):
    brand_overview: str
    main_category: str = ""
    main_products_services: list[str] = Field(default_factory=list)
    competitors: list[LLMCompetitor] = Field(default_factory=list)
    comparison: list[ComparisonEntry] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list, description="Competitive advantages of the brand")
    opportunities: list[str] = Field(default_factory=list, description="Ways to improve market positioning")

    @field_validator("competitors")
    @classmethod
    def _validate_competitors(cls, v):
        if len(v) > 5:
            raise ValueError(f"competitors cannot contain more than 5 items, got {len(v)}")
        return v

    @model_validator(mode="after")
    def _align_comparison(self) -> Self:
        if not self.competitors:
            self.comparison = [c for c in self.comparison if c.role == "your_brand"]
        else:
            comp_names = {c.name.strip().lower() for c in self.competitors if c.name}
            aligned = [
                c for c in self.comparison
                if c.role == "your_brand" or c.brand_name.strip().lower() in comp_names
            ]
            if not any(c.role == "competitor" for c in aligned) and any(c.role == "competitor" for c in self.comparison):
                comp_rows = [c for c in self.comparison if c.role == "competitor"][:len(self.competitors)]
                aligned = [c for c in self.comparison if c.role == "your_brand"] + comp_rows
            self.comparison = aligned
        return self


_QUERY_STOP_WORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "by", "for", "with", "about",
    "to", "from", "of", "best", "top", "cheap", "affordable", "friendly", "near",
    "local", "online", "good", "great", "my", "your",
}


def _derive_platform_variant(
    platform: Platform,
    base_query: str,
    *,
    brand_name: str = "",
    category: str = "",
    location: str | None = None,
) -> PlatformQueryVariant:
    import re
    clean_q = base_query.strip()
    words = [w for w in re.findall(r"[A-Za-z0-9]+", clean_q) if len(w) >= 2]
    substantive = [w for w in words if w.lower() not in _QUERY_STOP_WORDS] or words

    # Extract brand tokens to avoid meaningless brand-only hashtags on Instagram
    brand_tokens = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", brand_name)} if brand_name else set()

    if platform == "Google":
        return PlatformQueryVariant(platform="Google", query=clean_q, format="search_phrase")
    elif platform == "Instagram":
        # Prefer service, category, and location terms; avoid brand-only hashtags
        non_brand = [w for w in substantive if w.lower() not in brand_tokens]
        chosen = non_brand if non_brand else substantive

        # If only brand tokens were present and category is known, incorporate category tokens
        if not non_brand and category:
            cat_words = [w for w in re.findall(r"[A-Za-z0-9]+", category) if w.lower() not in _QUERY_STOP_WORDS]
            if cat_words:
                chosen = cat_words

        compound = "".join(w.capitalize() for w in chosen[:3])
        if len(chosen) >= 3:
            category_tag = f"#{chosen[-2].capitalize()}{chosen[-1].capitalize()}"
            ht_query = f"#{compound} {category_tag}"
        else:
            ht_query = f"#{compound}" if compound else f"#{clean_q.replace(' ', '')}"
        return PlatformQueryVariant(platform="Instagram", query=ht_query, format="hashtag")
    elif platform == "Facebook":
        lower_q = clean_q.lower()
        if "recommendation" in lower_q or "reviews" in lower_q:
            fb_q = clean_q
        elif any(loc_kw in lower_q for loc_kw in ("local", "near me", "nearby", "community", "group")):
            fb_q = f"{clean_q} recommendations"
        elif any(term in lower_q for term in ("agency", "studio", "service", "services", "company", "clinic", "dentist", "plumber")):
            fb_q = f"{clean_q} recommendations local community"
        else:
            fb_q = f"{clean_q} recommendations local"
        return PlatformQueryVariant(platform="Facebook", query=fb_q, format="community_search")
    elif platform == "LinkedIn":
        lower_q = clean_q.lower()
        if "vendor" in lower_q or "provider" in lower_q:
            li_q = clean_q
        elif any(b2b_kw in lower_q for b2b_kw in ("b2b", "professional", "enterprise", "agency", "software", "saas", "solutions")):
            li_q = f"{clean_q} vendor"
        else:
            li_q = f"{clean_q} professional vendor"
        return PlatformQueryVariant(platform="LinkedIn", query=li_q, format="b2b_keyword")
    elif platform == "X":
        if substantive:
            tag = f"#{''.join(w.capitalize() for w in substantive[:2])}"
            x_query = f"{clean_q} {tag}"
        else:
            x_query = clean_q
        return PlatformQueryVariant(platform="X", query=x_query.strip(), format="topic_discussion")
    return PlatformQueryVariant(platform=platform, query=clean_q, format="search_phrase")


class PlatformQueryVariant(_Base):
    platform: Platform
    query: str
    format: Optional[str] = Field(None, description="e.g. 'search_phrase', 'hashtag', 'community_search', 'b2b_keyword', 'topic_discussion'")

    @field_validator("platform", mode="before")
    @classmethod
    def _normalise_platform(cls, v):
        mapped = _PLATFORM_ALIASES.get(str(v).strip().lower())
        return mapped or str(v)


class SearchQuery(_Base):
    query: str
    platforms: list[Platform]
    intent: str
    business_relevance: str = ""
    target_audience: str = ""
    platform_queries: list[PlatformQueryVariant] = Field(default_factory=list)
    provenance: Provenance = "recommendation"

    @field_validator("platforms", mode="before")
    @classmethod
    def _normalise_platforms(cls, v):
        if isinstance(v, str):
            v = [v]
        out: list[str] = []
        for item in v or []:
            mapped = _PLATFORM_ALIASES.get(str(item).strip().lower())
            if mapped and mapped not in out:
                out.append(mapped)
        if not out:
            raise ValueError("at least one supported platform (Google, Facebook, Instagram, LinkedIn, X) is required")
        return out

    @model_validator(mode="after")
    def _ensure_platform_queries(self) -> Self:
        if not self.platform_queries and self.platforms:
            self.platform_queries = [_derive_platform_variant(p, self.query) for p in self.platforms]
            return self

        # Keep platforms and platform_queries strictly consistent
        for pq in self.platform_queries:
            if pq.platform not in self.platforms:
                self.platforms.append(pq.platform)

        existing = {pq.platform: pq for pq in self.platform_queries}
        aligned_pqs: list[PlatformQueryVariant] = []
        for p in self.platforms:
            if p in existing:
                aligned_pqs.append(existing[p])
            else:
                aligned_pqs.append(_derive_platform_variant(p, self.query))
        self.platform_queries = aligned_pqs
        return self


class SupportingContent(_Base):
    type: ContentType = "other"
    title: str
    rationale: str = ""
    provenance: Provenance = "recommendation"

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v):
        text = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        allowed = set(ContentType.__args__)  # type: ignore[attr-defined]
        return text if text in allowed else "other"


class SeoLLM(_Base):
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    long_tail_keywords: list[str] = Field(default_factory=list)
    business_keywords: list[str] = Field(default_factory=list)
    recommended_title: str
    title_primary_keyword: str = ""
    title_structure: str = Field("", description="Pattern used, e.g. 'Primary Keyword | Benefit | Brand'")
    meta_description: str
    meta_description_keywords: list[str] = Field(default_factory=list)
    supporting_content: list[SupportingContent] = Field(default_factory=list)


class Service(_Base):
    name: str
    description: str
    target_industry: str
    target_customer: str
    main_benefit: str
    source: ServiceSource = "website"
    provenance: Provenance = "inferred"
    description_provenance: Provenance = Field(
        "inferred",
        description="website_verified if description is directly grounded in website text, inferred if synthesized.",
    )
    evidence: list[Evidence] = Field(default_factory=list, max_length=5)

    @field_validator("source", mode="before")
    @classmethod
    def _source(cls, v):
        return "website" if str(v).strip().lower() in {"website", "site", "explicit", "stated"} else "inferred"


class MarketingSegment(_Base):
    audience: str
    business_type: str = ""
    requirement: str = ""
    provenance: Provenance = "recommendation"


class TargetAudience(_Base):
    primary_audience: list[str]
    secondary_audience: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    business_types: list[str] = Field(default_factory=list)
    customer_profiles: list[str] = Field(default_factory=list)
    geographic_focus: Optional[str] = None
    geographic_focus_provenance: Optional[Provenance] = None
    geographic_focus_evidence: list[Evidence] = Field(default_factory=list, max_length=5)
    audience_provenance: Provenance = "inferred"
    customer_needs: list[str] = Field(default_factory=list)
    customer_challenges: list[str] = Field(default_factory=list)
    customer_interests: list[str] = Field(default_factory=list)
    recommended_marketing_segments: list[MarketingSegment] = Field(default_factory=list)


class LLMAnalysis(_Base):
    """Exactly what the LLM is asked to return (validated strictly)."""

    brand: Brand
    competitor_analysis: CompetitorAnalysisLLM
    target_search_queries: list[SearchQuery]
    seo_improvements: SeoLLM
    top_services: list[Service] = Field(default_factory=list)
    target_audience: TargetAudience

    @model_validator(mode="before")
    @classmethod
    def _trim_to_five(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            data["target_search_queries"] = _truncate_list(data.get("target_search_queries"), 5)
        return data

    @field_validator("target_search_queries")
    @classmethod
    def _five_queries(cls, v):
        if len(v) != 5:
            raise ValueError(f"target_search_queries must contain exactly 5 items, got {len(v)}")
        return v

    @field_validator("top_services")
    @classmethod
    def _validate_services(cls, v):
        if len(v) > 5:
            raise ValueError(f"top_services cannot contain more than 5 items, got {len(v)}")
        return v


# ------------------------------------------------------------------ response models (LLM + server facts)
class CompetitorRelevance(_Base):
    status: RelevanceStatus = "not_checked"
    competitor_type: CompetitorType = "unknown"
    signals: list[str] = Field(
        default_factory=list,
        description="Observed market overlap signals: category_overlap, offering_overlap, audience_overlap, geographic_overlap",
    )
    explanation: str = Field(
        "",
        description="Analytical explanation of candidate relevance based on actual fetched competitor website content.",
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        max_length=5,
        description="Excerpts from the competitor's fetched website supporting the assessment.",
    )


class Competitor(LLMCompetitor):
    source: Literal["ai_suggested"] = "ai_suggested"
    verification: Verification = "not_checked"
    verified_page_title: Optional[str] = None
    relevance: CompetitorRelevance = Field(default_factory=CompetitorRelevance)


class CompetitorAnalysis(CompetitorAnalysisLLM):
    competitors: list[Competitor] = Field(default_factory=list)
    research_note: str = (
        "Competitors are AI-generated suggestions, not search-engine research. 'verification' "
        "indicates whether the suggested website is live and mentions the competitor's name. 'relevance' "
        "evaluates category, offering, audience, and geographic overlap based on actual fetched page content."
    )

    @model_validator(mode="after")
    def _align_comparison(self) -> Self:
        # Only candidates evaluated as likely_relevant or possible are eligible for direct comparison
        eligible_names = {
            c.name.strip().lower()
            for c in self.competitors
            if getattr(c, "relevance", None) is not None
            and c.relevance.status in ("likely_relevant", "possible")
        }
        eligible_hosts = {
            (urlsplit(c.website).hostname or "").lower().removeprefix("www.")
            for c in self.competitors
            if c.website
            and getattr(c, "relevance", None) is not None
            and c.relevance.status in ("likely_relevant", "possible")
        }
        self.comparison = [
            c for c in self.comparison
            if c.role == "your_brand"
            or (
                c.brand_name.strip().lower() in eligible_names
                or any(h and h in c.brand_name.strip().lower() for h in eligible_hosts)
            )
        ]
        return self


class SeoCheck(BaseModel):
    check: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class CurrentPageSeo(BaseModel):
    title: Optional[str] = None
    meta_description: Optional[str] = None
    h1: list[str] = Field(default_factory=list)
    canonical: Optional[str] = None
    word_count_sampled: int = 0


class SeoImprovements(SeoLLM):
    recommended_title_length: int = 0
    meta_description_length: int = 0
    current_page: CurrentPageSeo = Field(default_factory=CurrentPageSeo)
    on_page_checks: list[SeoCheck] = Field(default_factory=list)


class Analysis(BaseModel):
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery]
    seo_improvements: SeoImprovements
    top_services: list[Service]
    target_audience: TargetAudience


class Meta(BaseModel):
    provider: str
    model: str
    cached: bool = False
    pages_analyzed: list[str] = Field(default_factory=list)
    content_chars_sent: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    llm_calls: int = 1
    repaired_output: bool = False
    fallback_used: bool = False
    timings_ms: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class LinkAnalysisResponse(BaseModel):
    success: bool = True
    website: str
    brand: Brand
    analysis: Analysis
    meta: Meta


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorBody
