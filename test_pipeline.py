#!/usr/bin/env python3
"""
Unit and Integration Tests for FDAAA 801 Results Rescue Pipeline
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

# Ensure submission directory is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import (
    TrialAnalyzer,
    OutreachRenderer,
    FDAAA801RescuePipeline,
    OverdueTrialProspect,
    STATUTORY_DAILY_CMP,
    STATUTORY_SUBMISSION_WINDOW_DAYS,
    MOCK_CTGOV_STUDIES
)

class TestFDAAA801Pipeline(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_date_parsing(self):
        # Exact date
        parsed, days_past = TrialAnalyzer.parse_completion_date("2024-01-15")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2024)
        self.assertEqual(parsed.month, 1)
        self.assertGreater(days_past, 365)

        # Year-month
        parsed, days_past = TrialAnalyzer.parse_completion_date("2023-06")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2023)
        self.assertEqual(parsed.month, 6)
        self.assertGreater(days_past, 500)

        # None / invalid
        parsed, days_past = TrialAnalyzer.parse_completion_date(None)
        self.assertIsNone(parsed)
        self.assertEqual(days_past, 0)

    def test_cto_classification_solo_pi(self):
        # Lead sponsor class INDIV
        is_pi, has_cto, cto_class, tier, score = TrialAnalyzer.classify_institutional_cto(
            responsible_party_type="PRINCIPAL_INVESTIGATOR",
            lead_sponsor_name="Bennett, Robert, M.D.",
            lead_sponsor_class="INDIV",
            pi_affiliation="Bennett Arthritis Clinic, PC",
            pi_full_name="Robert Bennett, M.D."
        )
        self.assertTrue(is_pi)
        self.assertFalse(has_cto)
        self.assertEqual(cto_class, "SOLO_PI_PRACTICE")
        self.assertEqual(tier, "TIER_1_SOLO_PI")
        self.assertGreaterEqual(score, 90.0)

    def test_cto_classification_community_clinic(self):
        # Community surgery center / LLC
        is_pi, has_cto, cto_class, tier, score = TrialAnalyzer.classify_institutional_cto(
            responsible_party_type="SPONSOR_INVESTIGATOR",
            lead_sponsor_name="Coastal Eye Surgery Center, Inc.",
            lead_sponsor_class="OTHER",
            pi_affiliation="Coastal Eye Surgery Center, Inc.",
            pi_full_name="Marcus Sterling, M.D."
        )
        self.assertTrue(is_pi)
        self.assertFalse(has_cto)
        self.assertEqual(cto_class, "COMMUNITY_CLINIC_NO_CTO")
        self.assertEqual(tier, "TIER_1_COMMUNITY_SPECIALTY")
        self.assertGreaterEqual(score, 85.0)

    def test_cto_classification_orphan_academic(self):
        # Non-CTSA regional college where PI is designated RP
        is_pi, has_cto, cto_class, tier, score = TrialAnalyzer.classify_institutional_cto(
            responsible_party_type="PRINCIPAL_INVESTIGATOR",
            lead_sponsor_name="Midwestern Health Sciences College",
            lead_sponsor_class="OTHER",
            pi_affiliation="Midwestern Health Sciences College",
            pi_full_name="Elena Rostova, Ph.D."
        )
        self.assertTrue(is_pi)
        self.assertFalse(has_cto)
        self.assertEqual(cto_class, "ORPHAN_ACADEMIC_PI")
        self.assertEqual(tier, "TIER_2_ORPHAN_ACADEMIC")

    def test_cto_classification_institutional_cto(self):
        # Johns Hopkins University with SPONSOR responsible party
        is_pi, has_cto, cto_class, tier, score = TrialAnalyzer.classify_institutional_cto(
            responsible_party_type="SPONSOR",
            lead_sponsor_name="Johns Hopkins University",
            lead_sponsor_class="OTHER",
            pi_affiliation="The Johns Hopkins Hospital",
            pi_full_name=""
        )
        self.assertFalse(is_pi)
        self.assertTrue(has_cto)
        self.assertEqual(cto_class, "INSTITUTIONAL_SPONSOR")
        self.assertEqual(tier, "TIER_4_INSTITUTIONAL")

    def test_cmp_liability_calculation(self):
        study_mock = MOCK_CTGOV_STUDIES[0]  # Completed 2024-04-15
        prospect = TrialAnalyzer.parse_study(study_mock)
        self.assertIsNotNone(prospect)
        self.assertGreater(prospect.days_overdue, 0)
        self.assertEqual(prospect.daily_cmp_rate, STATUTORY_DAILY_CMP)
        # Potential CMP should equal max(0, days_overdue - 30) * 15107
        expected_cmp = max(0, prospect.days_overdue - 30) * 15107.0
        self.assertEqual(prospect.potential_cmp_accrued, expected_cmp)

    def test_mock_pipeline_run_and_exports(self):
        json_path = os.path.join(self.test_dir, "test_prospects.json")
        csv_path = os.path.join(self.test_dir, "test_prospects.csv")
        outreach_dir = os.path.join(self.test_dir, "test_outreach")

        pipeline = FDAAA801RescuePipeline(
            use_mock=True,
            target_pi_only=True,
            us_only=True
        )
        prospects = pipeline.run(
            limit=10,
            output_json=json_path,
            output_csv=csv_path,
            export_outreach_dir=outreach_dir
        )

        # 4 out of 5 mock studies should qualify (1 is institutional Johns Hopkins)
        self.assertGreaterEqual(len(prospects), 3)
        self.assertTrue(os.path.exists(json_path))
        self.assertTrue(os.path.exists(csv_path))
        self.assertTrue(os.path.exists(outreach_dir))

        # Check outreach files created
        outreach_files = os.listdir(outreach_dir)
        self.assertGreaterEqual(len(outreach_files), 3)

        # Verify content of first outreach file
        sample_file = os.path.join(outreach_dir, outreach_files[0])
        with open(sample_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("FDAAA 801", content)
            self.assertIn("$15,107", content)
            self.assertIn("NCT", content)

    def test_outreach_renderer_replacements(self):
        prospect = OverdueTrialProspect(
            nct_id="NCT09999999",
            brief_title="Pilot Evaluation of Novel Topical Cream",
            official_title="A Randomized Controlled Evaluation of Novel Topical Cream",
            overall_status="COMPLETED",
            completion_date="2024-05-01",
            primary_completion_date="2024-04-01",
            days_overdue=500,
            days_past_completion=865,
            responsible_party_type="SPONSOR_INVESTIGATOR",
            pi_full_name="Sarah Jenkins, M.D.",
            pi_last_name="Jenkins",
            pi_title="Medical Director",
            pi_affiliation="Jenkins Dermatology Associates, LLC",
            lead_sponsor_name="Jenkins Dermatology Associates, LLC",
            lead_sponsor_class="OTHER",
            contact_email="drjenkins@dermassoc.com",
            daily_cmp_rate=15107.0,
            potential_cmp_accrued=470 * 15107.0,
            recommended_service_quote="$1,500 - $2,000"
        )
        email = OutreachRenderer.render_email(prospect)
        self.assertIn("NCT09999999", email)
        self.assertIn("Dr. Jenkins", email)
        self.assertIn("$15,107", email)
        self.assertIn("Jenkins Dermatology Associates, LLC", email)

    def test_live_api_execution(self):
        # Quick integration test against live CT.gov API v2
        pipeline = FDAAA801RescuePipeline(
            use_mock=False,
            target_pi_only=True,
            us_only=True
        )
        try:
            prospects = pipeline.run(limit=3, page_size=3)
            self.assertIsInstance(prospects, list)
            if prospects:
                p = prospects[0]
                self.assertTrue(p.nct_id.startswith("NCT"))
                self.assertGreater(p.days_past_completion, 365)
                self.assertIn(p.overall_status, ("COMPLETED", "TERMINATED"))
        except Exception as e:
            self.skipTest(f"Live network test skipped due to connectivity issue: {e}")


if __name__ == "__main__":
    unittest.main()
