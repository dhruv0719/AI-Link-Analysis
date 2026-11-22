"""Pydantic models: API request/response and the structure the LLM must return."""
from __future__ import annotations

from typing import Any, Literal, Optional, Self

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
        description="Overall brand provenance (website_verified only if brand name is grounded in page metadata/content)."
    )
    name_provenance: Provenance = Field(
        "inferred",
        description="Provenance of the brand name itself."
    )
    summary_provenance: Provenance = Field(
        "inferred",
        description="Provenance of the brand summary."
    )
    evidence: list[Evidence] = Field(default_factory=list, max_length=5)


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


class SearchQuery(_Base):
    query: str
    platforms: list[Platform]
    intent: str
    business_relevance: str = ""
    target_audience: str = ""
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
class Competitor(LLMCompetitor):
    source: Literal["ai_suggested"] = "ai_suggested"
    verification: Verification = "not_checked"
    verified_page_title: Optional[str] = None


class CompetitorAnalysis(CompetitorAnalysisLLM):
    competitors: list[Competitor] = Field(default_factory=list)
    research_note: str = (
        "Competitors are AI-generated suggestions, not search-engine research. 'verification' only "
        "shows whether the suggested website is live and mentions the competitor's name."
    )


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
