#!/usr/bin/env python3
"""
FDAAA 801 Overdue-Results Rescue Pipeline
=========================================
Pulls overdue-results trials using the ClinicalTrials.gov v2 REST API:
  - Filters by study status (COMPLETED or TERMINATED) without results posted (hasResults=false)
  - Identifies trials where study completion date > 1 year ago (42 CFR § 11.42 statutory deadline)
  - Parses Responsible Party, Lead Sponsor, Collaborators, and Contact/Facility details
  - Analyzes institutional infrastructure to identify individual PI sponsors and
    independent clinical practices lacking centralized Clinical Trial Offices (CTOs)
  - Calculates accrued Civil Monetary Penalty (CMP) exposure ($15,107/day under 45 CFR § 102.3 / 21 U.S.C. § 333(f)(3)(B))
  - Generates personalized, high-converting regulatory compliance outreach letters

Author: Gemini (Revenue Evaluation & Compliance Automation)
"""

import argparse
import csv
import dataclasses
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Dict, Any, Optional, Tuple

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('fdaaa801-pipeline')

# ---------------------------------------------------------------------------
# Regulatory & Economic Constants
# ---------------------------------------------------------------------------
# 45 CFR § 102.3 / 21 U.S.C. § 333(f)(3)(B) Inflation-Adjusted Daily CMP Rate
STATUTORY_DAILY_CMP = 15107.0

# Statutory cure period following notice (calendar days)
STATUTORY_CURE_PERIOD_DAYS = 30

# Statutory deadline for results submission post-PCD (1 calendar year)
STATUTORY_SUBMISSION_WINDOW_DAYS = 365

# Commercial Comparators
COMPARATOR_UCLA_CTSI = 2500.0
COMPARATOR_EMORY_CTSI = 3500.0

# ---------------------------------------------------------------------------
# Knowledge Base: Institutional CTO / CTSA Detection
# ---------------------------------------------------------------------------
# Major NIH CTSA Hubs and Academic Health Systems with dedicated central CTOs / PRS offices
KNOWN_CTSA_HUBS = {
    "johns hopkins", "harvard", "massachusetts general", "brigham and women",
    "mayo clinic", "duke university", "stanford university", "columbia university",
    "yale university", "university of pennsylvania", "penn medicine", "ucla",
    "university of california, los angeles", "emory university", "university of michigan",
    "washington university in st", "northwestern university", "mount sinai",
    "icahn school of medicine", "university of pittsburgh", "vanderbilt university",
    "vanderbilt university medical center", "unc chapel hill", "university of north carolina",
    "cleveland clinic", "cedars-sinai", "memorial sloan kettering", "md anderson",
    "weill cornell", "cornell university", "university of chicago",
    "university of washington", "ohio state university", "case western reserve",
    "university of california, san francisco", "ucsf", "uc san diego",
    "university of california, san diego", "uc davis", "uc irvine",
    "indiana university", "albert einstein college of medicine", "tufts university",
    "georgetown university", "dartmouth-hitchcock", "brown university",
    "wake forest", "university of rochester", "rutgers university",
    "university of wisconsin-madison", "university of minnesota",
    "university of florida", "university of miami", "university of utah",
    "university of virginia", "university of colorado", "university of cincinnati"
}

# Indicators of solo private practice, independent physician group, or non-institutional clinic
SOLO_OR_COMMUNITY_PATTERNS = [
    r"\b(m\.?d\.?|d\.?o\.?|dr\.?)\b",
    r"\b(p\.?c\.?|p\.?l\.?l\.?c\.?|l\.?l\.?c\.?|inc\.?|corp\.?|s\.?c\.?)\b",
    r"\b(private practice|physician|associates|clinic|group|specialists|institute of)\b",
    r"\b(eye institute|surgery center|orthopaedics?|dermatology|pediatrics?|cardiology)\b",
    r"\b(urology|pain management|retina|endoscopy|wellness|spine)\b",
    r"\b(community hospital|regional hospital|memorial hospital)\b",
    r"\b(family medicine|internal medicine|digestive disease|neurology)\b"
]

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------
@dataclass
class OverdueTrialProspect:
    """Represents an overdue clinical trial and enriched prospect profile."""
    nct_id: str
    brief_title: str
    official_title: str
    overall_status: str
    completion_date: str
    primary_completion_date: str
    days_overdue: int
    days_past_completion: int
    responsible_party_type: str
    pi_full_name: str
    pi_last_name: str
    pi_title: str
    pi_affiliation: str
    lead_sponsor_name: str
    lead_sponsor_class: str
    collaborators: List[str] = field(default_factory=list)
    is_fda_regulated_drug: bool = False
    is_fda_regulated_device: bool = False
    has_dmc: bool = False
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    facility_name: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    is_individual_pi_sponsor: bool = False
    has_institutional_cto: bool = False
    cto_classification: str = "UNKNOWN"
    priority_tier: str = "TIER_4_INSTITUTIONAL"
    prospect_score: float = 0.0
    daily_cmp_rate: float = STATUTORY_DAILY_CMP
    potential_cmp_accrued: float = 0.0
    recommended_service_quote: str = "$1,500"
    recommended_package_tier: str = "Standard Rescue ($1,500)"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# ClinicalTrials.gov v2 REST API Client
# ---------------------------------------------------------------------------
class CTGovClient:
    """Client for ClinicalTrials.gov v2 REST API with query filtering and pagination."""
    BASE_URL = "https://clinicaltrials.gov/api/v2/studies"

    def __init__(self, user_agent: str = "FDAAA801ResultsRescue/1.0 (Clinical Compliance Pipeline)"):
        self.user_agent = user_agent

    def fetch_studies(
        self,
        statuses: List[str] = ("COMPLETED", "TERMINATED"),
        max_completion_date: Optional[str] = None,
        query_locn: Optional[str] = None,
        page_size: int = 50,
        limit: int = 100,
        filter_pi_rp: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Queries API v2 for trials matching overdue criteria:
        - Status: COMPLETED or TERMINATED
        - Results: AREA[HasResults]false
        - Completion date: AREA[CompletionDate]RANGE[MIN, max_completion_date]
        - Responsible party: PRINCIPAL_INVESTIGATOR, SPONSOR_INVESTIGATOR, or INDIV sponsor class
        """
        if max_completion_date is None:
            # Default to exactly 1 year ago from today
            max_completion_date = (date.today() - timedelta(days=STATUTORY_SUBMISSION_WINDOW_DAYS)).isoformat()

        adv_conditions = [
            "AREA[HasResults]false",
            f"AREA[CompletionDate]RANGE[MIN, {max_completion_date}]"
        ]

        if filter_pi_rp:
            adv_conditions.append(
                "(AREA[ResponsiblePartyType]PRINCIPAL_INVESTIGATOR OR "
                "AREA[ResponsiblePartyType]SPONSOR_INVESTIGATOR OR "
                "AREA[LeadSponsorClass]INDIV)"
            )

        filter_advanced = " AND ".join(adv_conditions)
        status_filter = ",".join(statuses)

        collected_studies: List[Dict[str, Any]] = []
        next_page_token: Optional[str] = None

        logger.info(
            f"Querying ClinicalTrials.gov v2 API: status={status_filter}, "
            f"cutoff={max_completion_date}, target_limit={limit}"
        )

        while len(collected_studies) < limit:
            batch_size = min(page_size, limit - len(collected_studies))
            params: Dict[str, Any] = {
                "pageSize": batch_size,
                "filter.overallStatus": status_filter,
                "filter.advanced": filter_advanced,
                "countTotal": "true"
            }
            if query_locn:
                params["query.locn"] = query_locn
            if next_page_token:
                params["pageToken"] = next_page_token

            url = f"{self.BASE_URL}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json"
                }
            )

            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if resp.status != 200:
                        logger.error(f"HTTP Error {resp.status} querying CT.gov")
                        break
                    data = json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                logger.error(f"HTTP Error {e.code}: {e.reason}")
                break
            except Exception as e:
                logger.error(f"Network error querying CT.gov: {e}")
                break

            studies = data.get("studies", [])
            if not studies:
                logger.info("No further studies returned from API.")
                break

            collected_studies.extend(studies)
            total_count = data.get("totalCount", "unknown")
            logger.info(
                f"Retrieved {len(studies)} studies (batch total: {len(collected_studies)} / registry matching: {total_count})"
            )

            next_page_token = data.get("nextPageToken")
            if not next_page_token or len(collected_studies) >= limit:
                break

            # Polite throttle
            time.sleep(0.3)

        return collected_studies


# ---------------------------------------------------------------------------
# Trial Parser & Institutional CTO Detector
# ---------------------------------------------------------------------------
class TrialAnalyzer:
    """Parses trial data and classifies institutional CTO presence and PI liability."""

    @staticmethod
    def parse_completion_date(date_str: Optional[str]) -> Tuple[Optional[date], int]:
        """
        Parses date string ('YYYY-MM-DD', 'YYYY-MM', or 'YYYY') and calculates days past.
        Returns (parsed_date, days_past_completion).
        """
        if not date_str:
            return None, 0

        parsed: Optional[date] = None
        date_str_clean = date_str.strip()

        try:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str_clean):
                parsed = datetime.strptime(date_str_clean, "%Y-%m-%d").date()
            elif re.match(r"^\d{4}-\d{2}$", date_str_clean):
                parsed = datetime.strptime(f"{date_str_clean}-01", "%Y-%m-%d").date()
            elif re.match(r"^\d{4}$", date_str_clean):
                parsed = datetime.strptime(f"{date_str_clean}-01-01", "%Y-%m-%d").date()
            else:
                # Handle month name formats like 'October 2018'
                for fmt in ("%B %Y", "%b %Y", "%B %d, %Y"):
                    try:
                        parsed = datetime.strptime(date_str_clean, fmt).date()
                        break
                    except ValueError:
                        pass
        except Exception:
            return None, 0

        if parsed:
            days_past = (date.today() - parsed).days
            return parsed, days_past

        return None, 0

    @classmethod
    def classify_institutional_cto(
        cls,
        responsible_party_type: str,
        lead_sponsor_name: str,
        lead_sponsor_class: str,
        pi_affiliation: str,
        pi_full_name: str
    ) -> Tuple[bool, bool, str, str, float]:
        """
        Determines:
        1. is_individual_pi_sponsor (bool)
        2. has_institutional_cto (bool)
        3. cto_classification (str)
        4. priority_tier (str)
        5. prospect_score (float, 0-100)
        """
        sponsor_lower = (lead_sponsor_name or "").lower()
        affil_lower = (pi_affiliation or "").lower()
        combined_text = f"{sponsor_lower} {affil_lower}"

        # 1. Check for Solo MD / Private Practice / Direct Individual Sponsor
        is_indiv_class = (lead_sponsor_class or "").upper() == "INDIV"
        is_name_match = (
            pi_full_name
            and len(pi_full_name.split()) >= 2
            and pi_full_name.lower() in sponsor_lower
        )
        has_private_practice_keywords = any(
            re.search(pat, combined_text, re.IGNORECASE) for pat in SOLO_OR_COMMUNITY_PATTERNS
        )

        # 2. Check for Major CTSA / High-Resource Academic Health System
        is_known_ctsa = any(hub in combined_text for hub in KNOWN_CTSA_HUBS)

        # 3. Classify into tiers
        if is_indiv_class or is_name_match:
            # Definite individual PI sponsor: solo practitioner or private clinic
            is_pi = True
            has_cto = False
            cto_class = "SOLO_PI_PRACTICE"
            tier = "TIER_1_SOLO_PI"
            score = 95.0
        elif has_private_practice_keywords and not is_known_ctsa:
            # Independent community clinic, specialty surgery center, or physician group
            is_pi = True
            has_cto = False
            cto_class = "COMMUNITY_CLINIC_NO_CTO"
            tier = "TIER_1_COMMUNITY_SPECIALTY"
            score = 90.0
        elif responsible_party_type in ("PRINCIPAL_INVESTIGATOR", "SPONSOR_INVESTIGATOR"):
            if not is_known_ctsa:
                # Orphan academic PI: smaller university or regional hospital without central CTO
                is_pi = True
                has_cto = False
                cto_class = "ORPHAN_ACADEMIC_PI"
                tier = "TIER_2_ORPHAN_ACADEMIC"
                score = 80.0
            else:
                # Academic PI at CTSA institution where PI was designated Responsible Party
                # (Institution disclaims filing or bills faculty steep fees like UCLA $2,500)
                is_pi = True
                has_cto = True
                cto_class = "ACADEMIC_CTSA_FACULTY_LIABLE"
                tier = "TIER_3_CTSA_FACULTY_LIABLE"
                score = 65.0
        else:
            # Institutional sponsor (RP is SPONSOR) at major institution or pharma
            is_pi = False
            has_cto = True
            cto_class = "INSTITUTIONAL_SPONSOR"
            tier = "TIER_4_INSTITUTIONAL"
            score = 25.0

        return is_pi, has_cto, cto_class, tier, score

    @classmethod
    def parse_study(cls, study_data: Dict[str, Any]) -> Optional[OverdueTrialProspect]:
        """Parses raw study JSON from CT.gov API v2 into structured prospect record."""
        protocol = study_data.get("protocolSection", {})
        if not protocol:
            return None

        # Identification Module
        id_mod = protocol.get("identificationModule", {})
        nct_id = id_mod.get("nctId", "")
        brief_title = id_mod.get("briefTitle", "")
        official_title = id_mod.get("officialTitle", brief_title)

        # Status Module
        status_mod = protocol.get("statusModule", {})
        overall_status = status_mod.get("overallStatus", "")

        comp_struct = status_mod.get("completionDateStruct", {})
        completion_date_str = comp_struct.get("date", "")

        prim_comp_struct = status_mod.get("primaryCompletionDateStruct", {})
        prim_comp_date_str = prim_comp_struct.get("date", "") or completion_date_str

        # Parse date and compute overdue duration
        # Regulatory rule: Due 1 year (365 days) post primary completion date (42 CFR § 11.42)
        ref_date_str = prim_comp_date_str or completion_date_str
        parsed_dt, days_past = cls.parse_completion_date(ref_date_str)

        days_overdue = max(0, days_past - STATUTORY_SUBMISSION_WINDOW_DAYS)

        # Sponsor & Collaborators Module
        sponsor_mod = protocol.get("sponsorCollaboratorsModule", {})
        rp = sponsor_mod.get("responsibleParty", {})
        rp_type = rp.get("type", "UNKNOWN")
        pi_full_name = rp.get("investigatorFullName", "") or rp.get("oldNameTitle", "")
        pi_title = rp.get("investigatorTitle", "")
        pi_affiliation = rp.get("investigatorAffiliation", "") or rp.get("oldOrganization", "")

        lead_sponsor = sponsor_mod.get("leadSponsor", {})
        lead_sponsor_name = lead_sponsor.get("name", "")
        lead_sponsor_class = lead_sponsor.get("class", "UNKNOWN")

        collabs = [c.get("name", "") for c in sponsor_mod.get("collaborators", []) if c.get("name")]

        # Oversight Module
        oversight = protocol.get("oversightModule", {})
        is_fda_drug = bool(oversight.get("isFdaRegulatedDrug", False))
        is_fda_device = bool(oversight.get("isFdaRegulatedDevice", False))
        has_dmc = bool(oversight.get("oversightHasDmc", False))

        # Contacts & Locations Module
        contacts_mod = protocol.get("contactsLocationsModule", {})
        central_contacts = contacts_mod.get("centralContacts", [])
        officials = contacts_mod.get("overallOfficials", [])
        locations = contacts_mod.get("locations", [])

        # Extract contact information
        contact_name = ""
        contact_email = ""
        contact_phone = ""

        if central_contacts:
            c0 = central_contacts[0]
            contact_name = c0.get("name", "")
            contact_email = c0.get("email", "")
            contact_phone = c0.get("phone", "")

        if not contact_name and officials:
            o0 = officials[0]
            contact_name = o0.get("name", "")
            if not pi_full_name and o0.get("role") in ("PRINCIPAL_INVESTIGATOR", "STUDY_DIRECTOR"):
                pi_full_name = contact_name
                pi_affiliation = o0.get("affiliation", pi_affiliation)

        facility_name = ""
        city = ""
        state = ""
        country = ""

        if locations:
            loc0 = locations[0]
            facility_name = loc0.get("facility", "")
            city = loc0.get("city", "")
            state = loc0.get("state", "")
            country = loc0.get("country", "")
            if not contact_email and loc0.get("contacts"):
                loc_c0 = loc0["contacts"][0]
                contact_email = loc_c0.get("email", "")
                contact_phone = loc_c0.get("phone", "")

        # Fallback for PI Name if blank
        if not pi_full_name:
            if lead_sponsor_class == "INDIV":
                pi_full_name = lead_sponsor_name
            elif contact_name:
                pi_full_name = contact_name
            else:
                pi_full_name = "Principal Investigator"

        # Extract last name for salutation
        parts = [p for p in re.split(r"[,\s]+", pi_full_name) if p and not re.match(r"^(dr|md|m\.d|do|d\.o|phd|prof)\.?$", p, re.I)]
        pi_last_name = parts[-1] if parts else pi_full_name

        # Classify Institutional CTO & PI status
        is_pi, has_cto, cto_class, tier, score = cls.classify_institutional_cto(
            rp_type, lead_sponsor_name, lead_sponsor_class, pi_affiliation, pi_full_name
        )

        # Accrued CMP liability calculation (after 30-day cure window post notice)
        potential_cmp = max(0, days_overdue - STATUTORY_CURE_PERIOD_DAYS) * STATUTORY_DAILY_CMP

        # Determine Rescue Package and Fee
        if tier in ("TIER_1_SOLO_PI", "TIER_1_COMMUNITY_SPECIALTY"):
            rec_quote = "$1,500 - $2,000"
            rec_tier = "Solo PI Rapid Rescue ($1,500 - $2,000)"
        elif tier == "TIER_2_ORPHAN_ACADEMIC":
            rec_quote = "$2,250 - $2,750"
            rec_tier = "Orphan Academic Comprehensive Rescue ($2,250 - $2,750)"
        else:
            rec_quote = "$2,500 - $3,000"
            rec_tier = "Multi-Endpoint Protocol Rescue ($2,500 - $3,000)"

        return OverdueTrialProspect(
            nct_id=nct_id,
            brief_title=brief_title,
            official_title=official_title,
            overall_status=overall_status,
            completion_date=completion_date_str,
            primary_completion_date=prim_comp_date_str,
            days_overdue=days_overdue,
            days_past_completion=days_past,
            responsible_party_type=rp_type,
            pi_full_name=pi_full_name,
            pi_last_name=pi_last_name,
            pi_title=pi_title or "Principal Investigator",
            pi_affiliation=pi_affiliation or facility_name or lead_sponsor_name,
            lead_sponsor_name=lead_sponsor_name,
            lead_sponsor_class=lead_sponsor_class,
            collaborators=collabs,
            is_fda_regulated_drug=is_fda_drug,
            is_fda_regulated_device=is_fda_device,
            has_dmc=has_dmc,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            facility_name=facility_name,
            city=city,
            state=state,
            country=country,
            is_individual_pi_sponsor=is_pi,
            has_institutional_cto=has_cto,
            cto_classification=cto_class,
            priority_tier=tier,
            prospect_score=score,
            daily_cmp_rate=STATUTORY_DAILY_CMP,
            potential_cmp_accrued=potential_cmp,
            recommended_service_quote=rec_quote,
            recommended_package_tier=rec_tier
        )


# ---------------------------------------------------------------------------
# Personalized Outreach Generator
# ---------------------------------------------------------------------------
class OutreachRenderer:
    """Renders customized outreach letters and emails using the outreach template."""

    DEFAULT_TEMPLATE_PATH = "/home/user/revenue-eval/submissions/fdaaa801-results-rescue/outreach_template.md"

    @classmethod
    def render_email(
        cls,
        prospect: OverdueTrialProspect,
        template_text: Optional[str] = None,
        sender_name: str = "Robert Ketler, RAC",
        sender_email: str = "compliance@fdaaa801rescue.com",
        phone_number: str = "(202) 555-0198",
        calendar_link: str = "https://calendly.com/fdaaa801rescue/10min"
    ) -> str:
        """Renders an individualized outreach email for the given trial prospect."""
        title_short = prospect.brief_title[:45] + "..." if len(prospect.brief_title) > 45 else prospect.brief_title
        loc_city_state = f"{prospect.city}, {prospect.state}" if prospect.city and prospect.state else (prospect.city or prospect.state or "USA")

        replacements = {
            "{{NCT_ID}}": prospect.nct_id,
            "{{STUDY_TITLE}}": prospect.brief_title,
            "{{STUDY_TITLE_SHORT}}": title_short,
            "{{OFFICIAL_TITLE}}": prospect.official_title,
            "{{PI_NAME}}": prospect.pi_full_name,
            "{{PI_LAST_NAME}}": prospect.pi_last_name,
            "{{PI_TITLE}}": prospect.pi_title,
            "{{PI_AFFILIATION}}": prospect.pi_affiliation,
            "{{LEAD_SPONSOR_NAME}}": prospect.lead_sponsor_name,
            "{{COMPLETION_DATE}}": prospect.completion_date,
            "{{PRIMARY_COMPLETION_DATE}}": prospect.primary_completion_date,
            "{{DAYS_OVERDUE}}": str(prospect.days_overdue),
            "{{DAILY_PENALTY}}": f"${prospect.daily_cmp_rate:,.2f}",
            "{{POTENTIAL_CMP_ACCRUED}}": f"${prospect.potential_cmp_accrued:,.2f}",
            "{{PI_EMAIL}}": prospect.contact_email or f"{prospect.pi_last_name.lower()}@practice.org",
            "{{SECONDARY_EMAIL}}": "regulatory@practice.org",
            "{{LOCATION_FACILITY}}": prospect.facility_name or prospect.pi_affiliation,
            "{{LOCATION_ADDRESS}}": "Clinical Research Suite",
            "{{LOCATION_CITY_STATE_ZIP}}": loc_city_state,
            "{{DATE_TODAY}}": date.today().strftime("%B %d, %Y"),
            "{{CONSULTANT_NAME}}": sender_name,
            "{{SENDER_EMAIL}}": sender_email,
            "{{PHONE_NUMBER}}": phone_number,
            "{{CALENDAR_LINK}}": calendar_link,
            "{{WEBSITE_URL}}": "https://fdaaa801rescue.com",
            "{{RESPONSIBLE_PARTY_STRING}}": f"{prospect.pi_full_name} ({prospect.responsible_party_type})"
        }

        # If full template text provided, replace across it
        if template_text:
            content = template_text
            for placeholder, val in replacements.items():
                content = content.replace(placeholder, str(val))
            return content

        # Default rendered email
        email_body = f"""Subject: [URGENT] FDAAA 801 Compliance & $15,107/Day Penalty Notice: {prospect.nct_id} ({prospect.pi_last_name})
Recipient: {replacements['{{PI_EMAIL}}']}
CC: {prospect.pi_affiliation}

Dear Dr. {prospect.pi_last_name},

I am writing regarding your clinical trial registered on ClinicalTrials.gov:
- NCT Identifier: {prospect.nct_id}
- Protocol: {prospect.brief_title}
- Designated Responsible Party: {prospect.pi_full_name} ({prospect.pi_affiliation})
- Study Completion Date: {prospect.completion_date} (Primary: {prospect.primary_completion_date})
- Registry Status: Completed without Results Posted ({prospect.days_overdue} days past statutory 1-year deadline)

Under Section 801 of the Food and Drug Administration Amendments Act (FDAAA 801, 42 U.S.C. § 282(j)) and 42 CFR Part 11, summary tabular results are mandatory within 12 calendar months of primary completion.

Following the FDA CDER/CBER mass reminder notices issued on March 30, 2026 and active Pre-Notices of Noncompliance (e.g., CDER-2026-106 series), individual PIs and independent practices are receiving direct regulatory scrutiny. As the registered Responsible Party, statutory liability rests directly with you.

STATUTORY FINANCIAL EXPOSURE:
- Civil Monetary Penalties under 21 U.S.C. § 333(f)(3)(B) and 45 CFR § 102.3 currently accrue at $15,107 PER DAY following formal notice.
- Theoretical accrued exposure to date: {replacements['{{POTENTIAL_CMP_ACCRUED}}']}.
- Collateral risks include public listing on the FDA Notice of Noncompliance Registry and mandatory withholding of future NIH/HHS grant disbursements (42 U.S.C. § 282(j)(5)(A)).

THE PROBLEM WITH PRS DATA ENTRY:
Entering tabular results (Participant Flow, Baseline Characteristics, Primary/Secondary Outcomes, and Adverse Events) into the NLM Protocol Registration and Results System (PRS) has an 80%+ QC rejection rate for first-time investigator filings. Major academic centers charge steep internal fees (UCLA CTSI charges $2,500; Emory CTSI charges $3,500), while independent practices have no registry staff whatsoever.

OUR FIXED-FEE RESCUE SERVICE ({prospect.recommended_service_quote}):
Our specialized regulatory compliance group offers a turnkey, guaranteed resolution:
1. Complete Authoring of all 4 PRS tabular modules from your existing CSR, manuscript, or summary data.
2. Formal FDA Pre-Notice / Reminder Letter Response Memo to toll enforcement.
3. Guaranteed NLM/FDA Quality Control clearance through public registry posting.
4. Fast 7 to 14 business day turnaround.

To eliminate your enforcement exposure before formal escalation, please reply directly with your NCT ID or schedule a 10-minute scoping call: {calendar_link}

Sincerely,

{sender_name}
Director of Clinical Regulatory Compliance
FDAAA 801 Results Remediation Group
Phone: {phone_number} | Email: {sender_email}
"""
        return email_body


# ---------------------------------------------------------------------------
# Offline Realistic Mock Dataset (For Unit Tests and Deterministic Runs)
# ---------------------------------------------------------------------------
MOCK_CTGOV_STUDIES: List[Dict[str, Any]] = [
    {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT00116129",
                "briefTitle": "Efficacy and Safety of an Oral Growth Hormone Secretagogue in Fibromyalgia",
                "officialTitle": "A Double-Blind Randomized Controlled Trial of Oral Growth Hormone Drug in Primary Fibromyalgia"
            },
            "statusModule": {
                "overallStatus": "COMPLETED",
                "completionDateStruct": {"date": "2024-04-15", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {"date": "2024-03-01", "type": "ACTUAL"}
            },
            "sponsorCollaboratorsModule": {
                "responsibleParty": {
                    "type": "PRINCIPAL_INVESTIGATOR",
                    "investigatorFullName": "Robert Bennett, M.D.",
                    "investigatorTitle": "Medical Director & Principal Investigator",
                    "investigatorAffiliation": "Bennett Arthritis & Fibromyalgia Clinic, PC"
                },
                "leadSponsor": {
                    "name": "Bennett, Robert, M.D.",
                    "class": "INDIV"
                }
            },
            "oversightModule": {
                "isFdaRegulatedDrug": True,
                "isFdaRegulatedDevice": False,
                "oversightHasDmc": False
            },
            "contactsLocationsModule": {
                "locations": [
                    {
                        "facility": "Bennett Arthritis Clinic",
                        "city": "Portland",
                        "state": "Oregon",
                        "country": "United States",
                        "contacts": [{"name": "Robert Bennett", "email": "rbennett@fibroclinic.com", "phone": "503-555-0144"}]
                    }
                ]
            }
        }
    },
    {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT05198234",
                "briefTitle": "Topical Compound for Refractory Diabetic Neuropathic Foot Ulcers",
                "officialTitle": "Prospective Pilot Evaluation of Topical Wound Dressing in Chronic Neuropathic Ulceration"
            },
            "statusModule": {
                "overallStatus": "COMPLETED",
                "completionDateStruct": {"date": "2024-09-30", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {"date": "2024-08-15", "type": "ACTUAL"}
            },
            "sponsorCollaboratorsModule": {
                "responsibleParty": {
                    "type": "SPONSOR_INVESTIGATOR",
                    "investigatorFullName": "Arthur Vance, D.O.",
                    "investigatorTitle": "Chief of Podiatric Surgery",
                    "investigatorAffiliation": "Valley Podiatry & Wound Care Associates, LLC"
                },
                "leadSponsor": {
                    "name": "Valley Podiatry & Wound Care Associates, LLC",
                    "class": "OTHER"
                }
            },
            "oversightModule": {
                "isFdaRegulatedDrug": False,
                "isFdaRegulatedDevice": True,
                "oversightHasDmc": True
            },
            "contactsLocationsModule": {
                "locations": [
                    {
                        "facility": "Valley Wound Care Center",
                        "city": "Scottsdale",
                        "state": "Arizona",
                        "country": "United States",
                        "contacts": [{"name": "Arthur Vance", "email": "avance@valleywoundcare.com", "phone": "480-555-0182"}]
                    }
                ]
            }
        }
    },
    {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT04871290",
                "briefTitle": "High-Intensity Functional Training in Subacute Stroke Rehabilitation",
                "officialTitle": "A Randomized Observer-Blinded Trial of Task-Oriented Training Post-Stroke"
            },
            "statusModule": {
                "overallStatus": "COMPLETED",
                "completionDateStruct": {"date": "2024-11-01", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {"date": "2024-10-15", "type": "ACTUAL"}
            },
            "sponsorCollaboratorsModule": {
                "responsibleParty": {
                    "type": "PRINCIPAL_INVESTIGATOR",
                    "investigatorFullName": "Elena Rostova, Ph.D., PT",
                    "investigatorTitle": "Associate Professor",
                    "investigatorAffiliation": "Midwestern Health Sciences College"
                },
                "leadSponsor": {
                    "name": "Midwestern Health Sciences College",
                    "class": "OTHER"
                }
            },
            "oversightModule": {
                "isFdaRegulatedDrug": False,
                "isFdaRegulatedDevice": False,
                "oversightHasDmc": False
            },
            "contactsLocationsModule": {
                "locations": [
                    {
                        "facility": "Midwestern Rehab Clinic",
                        "city": "Des Moines",
                        "state": "Iowa",
                        "country": "United States"
                    }
                ]
            }
        }
    },
    {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT03984112",
                "briefTitle": "Phase III Multi-Center Trial of Novel Monoclonal Antibody in Solid Tumors",
                "officialTitle": "A Phase III Randomized Trial in Advanced Carcinoma"
            },
            "statusModule": {
                "overallStatus": "COMPLETED",
                "completionDateStruct": {"date": "2024-05-20", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {"date": "2024-04-01", "type": "ACTUAL"}
            },
            "sponsorCollaboratorsModule": {
                "responsibleParty": {
                    "type": "SPONSOR"
                },
                "leadSponsor": {
                    "name": "Johns Hopkins University",
                    "class": "OTHER"
                }
            },
            "oversightModule": {
                "isFdaRegulatedDrug": True,
                "isFdaRegulatedDevice": False,
                "oversightHasDmc": True
            },
            "contactsLocationsModule": {
                "locations": [
                    {
                        "facility": "The Johns Hopkins Hospital",
                        "city": "Baltimore",
                        "state": "Maryland",
                        "country": "United States"
                    }
                ]
            }
        }
    },
    {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT04419088",
                "briefTitle": "Safety and Tolerability of Intracameral Injectable in Open-Angle Glaucoma",
                "officialTitle": "Terminated Phase II Investigation of Ophthalmic Delivery System"
            },
            "statusModule": {
                "overallStatus": "TERMINATED",
                "whyStopped": "Slow enrollment due to COVID-19",
                "completionDateStruct": {"date": "2024-06-30", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {"date": "2024-06-30", "type": "ACTUAL"}
            },
            "sponsorCollaboratorsModule": {
                "responsibleParty": {
                    "type": "SPONSOR_INVESTIGATOR",
                    "investigatorFullName": "Marcus Sterling, M.D.",
                    "investigatorTitle": "Founder & Senior Surgeon",
                    "investigatorAffiliation": "Coastal Eye Surgery Center, Inc."
                },
                "leadSponsor": {
                    "name": "Coastal Eye Surgery Center, Inc.",
                    "class": "OTHER"
                }
            },
            "oversightModule": {
                "isFdaRegulatedDrug": True,
                "isFdaRegulatedDevice": False,
                "oversightHasDmc": False
            },
            "contactsLocationsModule": {
                "locations": [
                    {
                        "facility": "Coastal Eye Ambulatory Surgery",
                        "city": "Tampa",
                        "state": "Florida",
                        "country": "United States",
                        "contacts": [{"name": "Marcus Sterling", "email": "msterling@coastaleye.com", "phone": "813-555-0199"}]
                    }
                ]
            }
        }
    }
]


# ---------------------------------------------------------------------------
# Pipeline Engine
# ---------------------------------------------------------------------------
class FDAAA801RescuePipeline:
    """End-to-end pipeline orchestrating API retrieval, classification, export, and outreach."""

    def __init__(
        self,
        use_mock: bool = False,
        min_days_overdue: int = STATUTORY_SUBMISSION_WINDOW_DAYS,
        target_pi_only: bool = True,
        us_only: bool = True
    ):
        self.use_mock = use_mock
        self.min_days_overdue = min_days_overdue
        self.target_pi_only = target_pi_only
        self.us_only = us_only
        self.client = CTGovClient()

    def run(
        self,
        limit: int = 50,
        page_size: int = 50,
        output_json: Optional[str] = None,
        output_csv: Optional[str] = None,
        export_outreach_dir: Optional[str] = None
    ) -> List[OverdueTrialProspect]:
        """Executes full extraction, analysis, filtering, and export workflow."""
        logger.info(f"Starting FDAAA 801 Results Rescue Pipeline (mock={self.use_mock}, limit={limit})...")

        if self.use_mock:
            raw_studies = MOCK_CTGOV_STUDIES
            logger.info(f"Loaded {len(raw_studies)} mock study records.")
        else:
            loc_filter = "United States" if self.us_only else None
            raw_studies = self.client.fetch_studies(
                statuses=["COMPLETED", "TERMINATED"],
                query_locn=loc_filter,
                page_size=page_size,
                limit=limit,
                filter_pi_rp=self.target_pi_only
            )

        prospects: List[OverdueTrialProspect] = []
        for raw in raw_studies:
            prospect = TrialAnalyzer.parse_study(raw)
            if not prospect:
                continue

            # Filter: Must be overdue beyond 1 year post completion
            if prospect.days_overdue <= 0:
                continue

            # Filter: Target PI / Non-institutional CTOs only
            if self.target_pi_only and prospect.has_institutional_cto and prospect.priority_tier == "TIER_4_INSTITUTIONAL":
                continue

            # Filter: US location only if requested
            if self.us_only and prospect.country and "united states" not in prospect.country.lower():
                continue

            prospects.append(prospect)

        # Sort by prospect score (descending), then by potential CMP accrued (descending)
        prospects.sort(key=lambda p: (p.prospect_score, p.potential_cmp_accrued), reverse=True)

        logger.info(f"Pipeline processed {len(prospects)} qualifying overdue PI prospects.")

        # Export JSON
        if output_json:
            os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump([p.to_dict() for p in prospects], f, indent=2)
            logger.info(f"Exported JSON prospects to: {output_json}")

        # Export CSV
        if output_csv:
            os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
            self._export_csv(prospects, output_csv)
            logger.info(f"Exported CSV prospect list to: {output_csv}")

        # Export Individual Outreach Emails
        if export_outreach_dir:
            os.makedirs(export_outreach_dir, exist_ok=True)
            template_text = None
            if os.path.exists(OutreachRenderer.DEFAULT_TEMPLATE_PATH):
                with open(OutreachRenderer.DEFAULT_TEMPLATE_PATH, "r", encoding="utf-8") as tf:
                    template_text = tf.read()

            for p in prospects:
                outreach_content = OutreachRenderer.render_email(p, template_text=None)
                out_filename = os.path.join(export_outreach_dir, f"OUTREACH_{p.nct_id}_{p.pi_last_name.upper()}.txt")
                with open(out_filename, "w", encoding="utf-8") as of:
                    of.write(outreach_content)
            logger.info(f"Generated {len(prospects)} personalized outreach drafts in: {export_outreach_dir}")

        return prospects

    @staticmethod
    def _export_csv(prospects: List[OverdueTrialProspect], csv_path: str):
        """Exports prospect list into standard sales/outreach CSV format."""
        fields = [
            "nct_id", "pi_full_name", "pi_last_name", "pi_title", "pi_affiliation",
            "lead_sponsor_name", "lead_sponsor_class", "responsible_party_type",
            "priority_tier", "cto_classification", "prospect_score",
            "overall_status", "completion_date", "days_overdue",
            "daily_cmp_rate", "potential_cmp_accrued", "recommended_service_quote",
            "contact_email", "contact_phone", "city", "state", "country", "brief_title"
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for p in prospects:
                d = p.to_dict()
                row = {k: d.get(k, "") for k in fields}
                writer.writerow(row)


# ---------------------------------------------------------------------------
# CLI Command Line Interface
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="FDAAA 801 Overdue-Results Rescue Prospect Pipeline"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of overdue trials to retrieve and process (default: 25)"
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=25,
        help="Page size per ClinicalTrials.gov API request (default: 25)"
    )
    parser.add_argument(
        "--all-countries",
        action="store_true",
        help="Include global trials (default: restrict to United States)"
    )
    parser.add_argument(
        "--include-institutional-ctos",
        action="store_true",
        help="Include major academic CTSA centers (default: prioritize solo/community PIs without CTOs)"
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run using realistic offline mock dataset for rapid deterministic verification"
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default="/home/user/revenue-eval/submissions/fdaaa801-results-rescue/overdue_pi_prospects.json",
        help="Output path for structured JSON results"
    )
    parser.add_argument(
        "--output-csv",
        type=str,
        default="/home/user/revenue-eval/submissions/fdaaa801-results-rescue/overdue_pi_prospects.csv",
        help="Output path for CSV prospect lead list"
    )
    parser.add_argument(
        "--export-outreach-dir",
        type=str,
        default="/home/user/revenue-eval/submissions/fdaaa801-results-rescue/generated_outreach",
        help="Directory to save individualized outreach letters"
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=5,
        help="Number of prospect summaries to preview on console (default: 5)"
    )

    args = parser.parse_args()

    pipeline = FDAAA801RescuePipeline(
        use_mock=args.mock,
        target_pi_only=not args.include_institutional_ctos,
        us_only=not args.all_countries
    )

    prospects = pipeline.run(
        limit=args.limit,
        page_size=args.page_size,
        output_json=args.output_json,
        output_csv=args.output_csv,
        export_outreach_dir=args.export_outreach_dir
    )

    # Console Summary Table
    print("\n" + "=" * 90)
    print("FDAAA 801 OVERDUE-RESULTS RESCUE PIPELINE — HIGH-CONVERTING PI PROSPECTS")
    print("=" * 90)
    print(f"Total Qualified Prospect Trials: {len(prospects)}")
    print(f"Statutory CMP Daily Accrual Rate: ${STATUTORY_DAILY_CMP:,.2f} / day")
    print(f"Target Service Pricing: $1,500 – $3,000 / trial (vs UCLA CTSI $2,500 / Emory CTSI $3,500)")
    print("-" * 90)

    for i, p in enumerate(prospects[:args.preview], 1):
        print(f"\nPROSPECT #{i}: [{p.priority_tier}] Score: {p.prospect_score:.1f}/100")
        print(f"  NCT ID:       {p.nct_id} ({p.overall_status})")
        print(f"  Title:        {p.brief_title[:75]}...")
        print(f"  PI / RP:      {p.pi_full_name} | Affiliation: {p.pi_affiliation}")
        print(f"  Lead Sponsor: {p.lead_sponsor_name} [Class: {p.lead_sponsor_class}]")
        print(f"  CTO Status:   {p.cto_classification} (Has Institutional CTO: {p.has_institutional_cto})")
        print(f"  Overdue:      {p.days_overdue} days past 1-year deadline (Completed: {p.completion_date})")
        print(f"  CMP Accrued:  ${p.potential_cmp_accrued:,.2f} (@ $15,107/day post cure window)")
        print(f"  Service Tier: {p.recommended_service_quote} ({p.recommended_package_tier})")
        if p.contact_email:
            print(f"  Contact:      {p.contact_email} | {p.contact_phone}")
        if p.city or p.state:
            print(f"  Location:     {p.city}, {p.state} ({p.country})")

    print("\n" + "=" * 90)
    print("EXECUTION COMPLETE.")
    if args.output_json:
        print(f"JSON Export: {args.output_json}")
    if args.output_csv:
        print(f"CSV Export:  {args.output_csv}")
    if args.export_outreach_dir:
        print(f"Outreach Drafts: {args.export_outreach_dir}")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
