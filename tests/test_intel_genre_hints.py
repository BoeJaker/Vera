"""Pages selling market-research reports are marketing, not research; real news and
analysis are left to the classifier."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.intel import intel_ops_core as ops  # noqa: E402

SELLERS = [
    "UK Commercial Real Estate Market Size & Trends 2031",
    "UK Commercial Real Estate Market - Industry Analysis, Size, Share, Growth, Trends, and Forecast 2025-2032",
    "UK Commercial Real Estate Market Size and Analysis, 2034",
    "UK Commercial Real Estate Market Report: Size, Growth, Trends ...",
    "Proptech Market to grow at a CAGR of 15.2% - request a free sample",
]
NOT_SELLERS = [
    "How U.S. Giant Realty Income Bought The UK And What It Plans Next",
    "Demand for tracker mortgages jumps as rates outlook shifts",
    "UK Real Estate Market Outlook Midyear Review 2026 | CBRE UK",
    "UK Commercial Property Update | Capital Economics",
    "UK house prices fall for first time since 2023, led by London",
    "Market share of AI agents grows as firms adopt them",          # 'market share' alone is news, not a report shop
]


def test_report_sellers_are_spotted_and_news_is_not():
    for t in SELLERS:
        assert ops.report_seller_hint(t), t
    for t in NOT_SELLERS:
        assert not ops.report_seller_hint(t), t
    assert ops.genre_factor("marketing") < 1.0                       # so they are demoted


def test_the_labels_no_longer_call_reports_research():
    assert "report" not in ops.GENRE_LABELS["research"]
    assert "market research report" in ops.GENRE_LABELS["marketing"]
