"""
Swarm Engine — turns natural-language prompts into screened stock baskets.

Flow:
    1. User POSTs a free-text prompt to /api/generate-asset/screen
    2. intent_parser() asks a local Ollama LLM to extract structured screening
       rules from the prompt.
    3. swarm_evaluator() filters against real fundamentals data via 4-path fallback:
       Path 1: FMP screener (all US stocks, needs API key)
       Path 2: S&P 500 fundamentals cache (yfinance/Yahoo v8 API)
       Path 3: Full universe cache (9,920+ SEC EDGAR tickers, progressively fetched)
       Path 4: Mock universe (~350 stocks, last resort)
    4. The matching tickers, parsed rules, and counts are returned as JSON.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    import fmp_client
except ImportError:
    fmp_client = None

try:
    import dexter_client
except ImportError:
    dexter_client = None

logger = logging.getLogger("swarm_engine")

# ---------------------------------------------------------------------------
# Mock universe — ~200 real tickers with synthetic fundamentals
# ---------------------------------------------------------------------------

MOCK_UNIVERSE: dict[str, dict[str, Any]] = {
    # ── Big-cap Tech ──────────────────────────────────────────────────────
    "AAPL":  {"market_cap": 2900, "fcf": 110000, "debt_to_equity": 1.73, "sector": "Technology",      "revenue_growth": 5.5,   "industry": "Consumer Electronics", "pe_ratio": 30, "roe": 25},
    "MSFT":  {"market_cap": 3100, "fcf": 63000,  "debt_to_equity": 0.35, "sector": "Technology",      "revenue_growth": 12.8,  "industry": "Software", "pe_ratio": 28, "roe": 20},
    "GOOGL": {"market_cap": 2100, "fcf": 69000,  "debt_to_equity": 0.05, "sector": "Technology",      "revenue_growth": 10.2,  "industry": "Internet Content", "pe_ratio": 22, "roe": 18},
    "AMZN":  {"market_cap": 1900, "fcf": 32000,  "debt_to_equity": 0.59, "sector": "Consumer Cyclical","revenue_growth": 11.8,  "industry": "Internet Retail", "pe_ratio": 25, "roe": 22},
    "META":  {"market_cap": 1400, "fcf": 43000,  "debt_to_equity": 0.26, "sector": "Technology",      "revenue_growth": 22.0,  "industry": "Internet Content", "pe_ratio": 35, "roe": 28},
    "NVDA":  {"market_cap": 3400, "fcf": 27000,  "debt_to_equity": 0.41, "sector": "Technology",      "revenue_growth": 122.0, "industry": "Semiconductors", "pe_ratio": 20, "roe": 15},
    "TSLA":  {"market_cap": 800,  "fcf": 4400,   "debt_to_equity": 0.08, "sector": "Consumer Cyclical","revenue_growth": 18.8,  "industry": "Auto Manufacturers", "pe_ratio": 22, "roe": 17},

    # ── Financials ────────────────────────────────────────────────────────
    "JPM":   {"market_cap": 580,  "fcf": 22000,  "debt_to_equity": 1.28, "sector": "Financial Services","revenue_growth": 8.4,  "industry": "Banks", "pe_ratio": 12, "roe": 16},
    "BAC":   {"market_cap": 310,  "fcf": 14000,  "debt_to_equity": 1.11, "sector": "Financial Services","revenue_growth": 3.9,  "industry": "Banks", "pe_ratio": 10, "roe": 14},
    "WFC":   {"market_cap": 200,  "fcf": 11000,  "debt_to_equity": 1.06, "sector": "Financial Services","revenue_growth": 2.1,  "industry": "Banks", "pe_ratio": 11, "roe": 13},

    # ── Healthcare ────────────────────────────────────────────────────────
    "JNJ":   {"market_cap": 380,  "fcf": 18000,  "debt_to_equity": 0.44, "sector": "Healthcare",      "revenue_growth": 4.2,   "industry": "Drug Manufacturers", "pe_ratio": 20, "roe": 12},
    "UNH":   {"market_cap": 480,  "fcf": 23000,  "debt_to_equity": 0.73, "sector": "Healthcare",      "revenue_growth": 13.6,  "industry": "Healthcare Plans", "pe_ratio": 25, "roe": 18},
    "PFE":   {"market_cap": 150,  "fcf": 5000,   "debt_to_equity": 0.82, "sector": "Healthcare",      "revenue_growth": -41.7, "industry": "Drug Manufacturers", "pe_ratio": 15, "roe": 10},

    # ── Energy ────────────────────────────────────────────────────────────
    "XOM":   {"market_cap": 460,  "fcf": 36000,  "debt_to_equity": 0.21, "sector": "Energy",          "revenue_growth": -5.3,  "industry": "Oil & Gas Integrated", "pe_ratio": 9, "roe": 12},
    "CVX":   {"market_cap": 300,  "fcf": 22000,  "debt_to_equity": 0.14, "sector": "Energy",          "revenue_growth": -10.8, "industry": "Oil & Gas Integrated", "pe_ratio": 8, "roe": 10},

    # ── Consumer Staples / Retail ─────────────────────────────────────────
    "HD":    {"market_cap": 370,  "fcf": 17000,  "debt_to_equity": 42.5, "sector": "Consumer Cyclical","revenue_growth": 2.8,   "industry": "Home Improvement Retail", "pe_ratio": 22, "roe": 14},
    "MCD":   {"market_cap": 210,  "fcf": 8000,   "debt_to_equity": -8.4, "sector": "Consumer Cyclical","revenue_growth": 10.0,  "industry": "Restaurants", "pe_ratio": 25, "roe": 16},
    "NKE":   {"market_cap": 140,  "fcf": 5600,   "debt_to_equity": 0.89, "sector": "Consumer Cyclical","revenue_growth": -1.7,  "industry": "Footwear & Accessories", "pe_ratio": 20, "roe": 15},
    "COST":  {"market_cap": 370,  "fcf": 7100,   "debt_to_equity": 0.31, "sector": "Consumer Defensive","revenue_growth": 9.4,  "industry": "Discount Stores", "pe_ratio": 23, "roe": 17},
    "WMT":   {"market_cap": 580,  "fcf": 12000,  "debt_to_equity": 0.53, "sector": "Consumer Defensive","revenue_growth": 6.0,  "industry": "Discount Stores", "pe_ratio": 24, "roe": 18},

    # ── Media / Entertainment ─────────────────────────────────────────────
    "DIS":   {"market_cap": 200,  "fcf": 8200,   "debt_to_equity": 0.42, "sector": "Communication Services","revenue_growth": 7.8,  "industry": "Entertainment", "pe_ratio": 22, "roe": 14},
    "NFLX":  {"market_cap": 310,  "fcf": 6900,   "debt_to_equity": 0.69, "sector": "Communication Services","revenue_growth": 12.5, "industry": "Entertainment", "pe_ratio": 25, "roe": 16},

    # ── Enterprise Software ────────────────────────────────────────────────
    "CRM":   {"market_cap": 290,  "fcf": 11500,  "debt_to_equity": 0.17, "sector": "Technology",      "revenue_growth": 11.2,  "industry": "Software", "pe_ratio": 20, "roe": 15},
    "ADBE":  {"market_cap": 240,  "fcf": 8500,   "debt_to_equity": 0.28, "sector": "Technology",      "revenue_growth": 10.5,  "industry": "Software", "pe_ratio": 22, "roe": 14},

    # ── Semiconductors ────────────────────────────────────────────────────
    "INTC":  {"market_cap": 110,  "fcf": -3500,  "debt_to_equity": 0.47, "sector": "Technology",      "revenue_growth": -15.8, "industry": "Semiconductors", "pe_ratio": 10, "roe": 12},
    "AMD":   {"market_cap": 220,  "fcf": 4800,   "debt_to_equity": 0.04, "sector": "Technology",      "revenue_growth": 9.6,   "industry": "Semiconductors", "pe_ratio": 12, "roe": 10},
    "QCOM":  {"market_cap": 190,  "fcf": 10200,  "debt_to_equity": 0.86, "sector": "Technology",      "revenue_growth": 1.3,   "industry": "Semiconductors", "pe_ratio": 15, "roe": 13},
    "TXN":   {"market_cap": 170,  "fcf": 6200,   "debt_to_equity": 0.66, "sector": "Technology",      "revenue_growth": -2.1,  "industry": "Semiconductors", "pe_ratio": 11, "roe": 11},
    "AVGO":  {"market_cap": 620,  "fcf": 18500,  "debt_to_equity": 1.64, "sector": "Technology",      "revenue_growth": 34.2,  "industry": "Semiconductors", "pe_ratio": 20, "roe": 16},

    # ── Fintech / Payments ────────────────────────────────────────────────
    "PYPL":  {"market_cap": 78,   "fcf": 5400,   "debt_to_equity": 0.49, "sector": "Financial Services","revenue_growth": 8.2,  "industry": "Credit Services", "pe_ratio": 12, "roe": 14},
    "SQ":    {"market_cap": 45,   "fcf": 1200,   "debt_to_equity": 0.53, "sector": "Financial Services","revenue_growth": 25.7, "industry": "Credit Services", "pe_ratio": 15, "roe": 16},

    # ── E-commerce / Gig Economy ──────────────────────────────────────────
    "SHOP":  {"market_cap": 130,  "fcf": 900,    "debt_to_equity": 0.11, "sector": "Technology",      "revenue_growth": 26.1,  "industry": "Software", "pe_ratio": 22, "roe": 14},
    "UBER":  {"market_cap": 155,  "fcf": 4300,   "debt_to_equity": 1.15, "sector": "Technology",      "revenue_growth": 16.9,  "industry": "Software", "pe_ratio": 25, "roe": 16},
    "ABNB":  {"market_cap": 95,   "fcf": 3800,   "debt_to_equity": 0.30, "sector": "Consumer Cyclical","revenue_growth": 18.1,  "industry": "Travel Services", "pe_ratio": 20, "roe": 14},

    # ── Crypto-adjacent ───────────────────────────────────────────────────
    "COIN":  {"market_cap": 55,   "fcf": 1600,   "debt_to_equity": 0.67, "sector": "Financial Services","revenue_growth": 101.4, "industry": "Capital Markets", "pe_ratio": 12, "roe": 14},

    # ── Data / Analytics / Cloud ──────────────────────────────────────────
    "PLTR":  {"market_cap": 62,   "fcf": 730,    "debt_to_equity": 0.0,  "sector": "Technology",      "revenue_growth": 20.8,  "industry": "Software", "pe_ratio": 22, "roe": 14},
    "SNOW":  {"market_cap": 68,   "fcf": 810,    "debt_to_equity": 0.0,  "sector": "Technology",      "revenue_growth": 32.9,  "industry": "Software", "pe_ratio": 25, "roe": 16},
    "NET":   {"market_cap": 35,   "fcf": 420,    "debt_to_equity": 1.29, "sector": "Technology",      "revenue_growth": 32.3,  "industry": "Software", "pe_ratio": 20, "roe": 14},
    "DDOG":  {"market_cap": 42,   "fcf": 680,    "debt_to_equity": 0.58, "sector": "Technology",      "revenue_growth": 25.6,  "industry": "Software", "pe_ratio": 22, "roe": 14},

    # ── Cybersecurity ─────────────────────────────────────────────────────
    "ZS":    {"market_cap": 30,   "fcf": 580,    "debt_to_equity": 0.43, "sector": "Technology",      "revenue_growth": 34.8,  "industry": "Software", "pe_ratio": 20, "roe": 14},
    "CRWD":  {"market_cap": 72,   "fcf": 930,    "debt_to_equity": 0.27, "sector": "Technology",      "revenue_growth": 36.4,  "industry": "Software", "pe_ratio": 22, "roe": 14},
    "PANW":  {"market_cap": 110,  "fcf": 3200,   "debt_to_equity": 1.85, "sector": "Technology",      "revenue_growth": 19.8,  "industry": "Software", "pe_ratio": 20, "roe": 14},

    # ── Ad-tech / Gaming ──────────────────────────────────────────────────
    "TTD":   {"market_cap": 48,   "fcf": 600,    "debt_to_equity": 0.07, "sector": "Technology",      "revenue_growth": 23.3,  "industry": "Software", "pe_ratio": 45, "roe": 20},
    "RBLX":  {"market_cap": 30,   "fcf": -200,   "debt_to_equity": 2.10, "sector": "Communication Services","revenue_growth": 25.0, "industry": "Electronic Gaming", "pe_ratio": 0, "roe": -15},
    "U":     {"market_cap": 14,   "fcf": -180,   "debt_to_equity": 1.40, "sector": "Technology",      "revenue_growth": -2.5,  "industry": "Software", "pe_ratio": 0, "roe": -8},

    # ── Fintech / Consumer Finance ────────────────────────────────────────
    "SOFI":  {"market_cap": 12,   "fcf": 350,    "debt_to_equity": 0.72, "sector": "Financial Services","revenue_growth": 34.5, "industry": "Credit Services", "pe_ratio": 28, "roe": 6},
    "HOOD":  {"market_cap": 20,   "fcf": 280,    "debt_to_equity": 0.31, "sector": "Financial Services","revenue_growth": 29.4, "industry": "Capital Markets", "pe_ratio": 35, "roe": 10},

    # ── EV ────────────────────────────────────────────────────────────────
    "RIVN":  {"market_cap": 15,   "fcf": -5400,  "debt_to_equity": 0.78, "sector": "Consumer Cyclical","revenue_growth": 167.4, "industry": "Auto Manufacturers"},
    "LCID":  {"market_cap": 7,    "fcf": -2800,  "debt_to_equity": 0.46, "sector": "Consumer Cyclical","revenue_growth": -4.1,  "industry": "Auto Manufacturers"},
    "NIO":   {"market_cap": 10,   "fcf": -3200,  "debt_to_equity": 1.02, "sector": "Consumer Cyclical","revenue_growth": 12.9,  "industry": "Auto Manufacturers"},

    # --- Manufacturing / Industrials (Small-Mid Cap) ---
    "RBC":   {"market_cap": 8.5,  "fcf": 250,    "debt_to_equity": 0.4,  "sector": "Industrials",     "revenue_growth": 8.2,   "industry": "Specialty Industrial Machinery", "pe_ratio": 22, "roe": 15},
    "GGG":   {"market_cap": 3.8,  "fcf": 120,    "debt_to_equity": 0.6,  "sector": "Industrials",     "revenue_growth": 5.1,   "industry": "Specialty Industrial Machinery", "pe_ratio": 28, "roe": 18},
    "AOS":   {"market_cap": 4.5,  "fcf": 180,    "debt_to_equity": 0.3,  "sector": "Industrials",     "revenue_growth": 6.4,   "industry": "Specialty Industrial Machinery", "pe_ratio": 20, "roe": 22},
    "GNRC":  {"market_cap": 7.2,  "fcf": 200,    "debt_to_equity": 1.1,  "sector": "Industrials",     "revenue_growth": -5.3,  "industry": "Specialty Industrial Machinery", "pe_ratio": 35, "roe": 12},
    "MIDD":  {"market_cap": 5.6,  "fcf": 160,    "debt_to_equity": 1.8,  "sector": "Industrials",     "revenue_growth": 3.2,   "industry": "Industrial Machinery", "pe_ratio": 14, "roe": 16},
    "WTS":   {"market_cap": 3.2,  "fcf": 90,     "debt_to_equity": 0.2,  "sector": "Industrials",     "revenue_growth": 7.1,   "industry": "Industrial Distribution", "pe_ratio": 18, "roe": 14},
    "MLI":   {"market_cap": 6.8,  "fcf": 280,    "debt_to_equity": 0.1,  "sector": "Industrials",     "revenue_growth": 12.5,  "industry": "Metal Fabrication", "pe_ratio": 16, "roe": 25},
    "ASTE":  {"market_cap": 0.8,  "fcf": 40,     "debt_to_equity": 0.3,  "sector": "Industrials",     "revenue_growth": 4.8,   "industry": "Farm & Heavy Construction Machinery", "pe_ratio": 12, "roe": 10},
    "NPO":   {"market_cap": 2.1,  "fcf": 70,     "debt_to_equity": 0.5,  "sector": "Industrials",     "revenue_growth": 9.3,   "industry": "Specialty Industrial Machinery", "pe_ratio": 24, "roe": 17},
    "EPAC":  {"market_cap": 4.3,  "fcf": 130,    "debt_to_equity": 2.5,  "sector": "Industrials",     "revenue_growth": 6.8,   "industry": "Specialty Chemicals", "pe_ratio": 19, "roe": 20},
    "SWK":   {"market_cap": 12.5, "fcf": 450,    "debt_to_equity": 1.2,  "sector": "Industrials",     "revenue_growth": -2.1,  "industry": "Tools & Accessories", "pe_ratio": 45, "roe": 5},
    "EMR":   {"market_cap": 65.0, "fcf": 2800,   "debt_to_equity": 0.5,  "sector": "Industrials",     "revenue_growth": 14.2,  "industry": "Industrial Automation", "pe_ratio": 25, "roe": 18},
    "ROK":   {"market_cap": 32.0, "fcf": 1200,   "debt_to_equity": 1.1,  "sector": "Industrials",     "revenue_growth": -3.5,  "industry": "Industrial Automation", "pe_ratio": 30, "roe": 15},
    "DOV":   {"market_cap": 22.0, "fcf": 900,    "debt_to_equity": 0.7,  "sector": "Industrials",     "revenue_growth": 5.8,   "industry": "Diversified Industrials", "pe_ratio": 17, "roe": 19},
    "ITW":   {"market_cap": 75.0, "fcf": 3200,   "debt_to_equity": 2.8,  "sector": "Industrials",     "revenue_growth": 1.2,   "industry": "Specialty Industrial Machinery", "pe_ratio": 23, "roe": 28},

    # --- Healthcare (Small-Mid Cap) ---
    "GMED":  {"market_cap": 4.1,  "fcf": 90,     "debt_to_equity": 0.4,  "sector": "Healthcare",      "revenue_growth": 11.2,  "industry": "Medical Devices", "pe_ratio": 22, "roe": 14},
    "IART":  {"market_cap": 2.8,  "fcf": 60,     "debt_to_equity": 0.7,  "sector": "Healthcare",      "revenue_growth": 5.6,   "industry": "Medical Instruments", "pe_ratio": 18, "roe": 11},
    "NVCR":  {"market_cap": 1.5,  "fcf": -30,    "debt_to_equity": 3.2,  "sector": "Healthcare",      "revenue_growth": 18.4,  "industry": "Medical Devices", "pe_ratio": 28, "roe": 8},
    "HALO":  {"market_cap": 6.2,  "fcf": 200,    "debt_to_equity": 0.9,  "sector": "Healthcare",      "revenue_growth": 22.1,  "industry": "Biotechnology", "pe_ratio": 25, "roe": 20},

    # --- Technology (Small-Mid Cap) ---
    "FOUR":  {"market_cap": 5.8,  "fcf": 180,    "debt_to_equity": 0.2,  "sector": "Technology",      "revenue_growth": 25.3,  "industry": "Software", "pe_ratio": 35, "roe": 22},
    "PCTY":  {"market_cap": 8.9,  "fcf": 250,    "debt_to_equity": 0.3,  "sector": "Technology",      "revenue_growth": 18.7,  "industry": "Software", "pe_ratio": 32, "roe": 20},
    "CWAN":  {"market_cap": 3.5,  "fcf": 80,     "debt_to_equity": 1.5,  "sector": "Technology",      "revenue_growth": 20.1,  "industry": "Software", "pe_ratio": 38, "roe": 18},
    "BRZE":  {"market_cap": 4.2,  "fcf": -10,    "debt_to_equity": 0.1,  "sector": "Technology",      "revenue_growth": 33.2,  "industry": "Software", "pe_ratio": 40, "roe": 15},
    "JAMF":  {"market_cap": 1.8,  "fcf": 50,     "debt_to_equity": 0.8,  "sector": "Technology",      "revenue_growth": 15.6,  "industry": "Software", "pe_ratio": 28, "roe": 17},

    # --- Consumer (Small-Mid Cap) ---
    "SHAK":  {"market_cap": 3.6,  "fcf": 45,     "debt_to_equity": 2.1,  "sector": "Consumer Cyclical","revenue_growth": 19.8,  "industry": "Restaurants", "pe_ratio": 30, "roe": 14},
    "WING":  {"market_cap": 8.2,  "fcf": 120,    "debt_to_equity": -5.0, "sector": "Consumer Cyclical","revenue_growth": 27.5,  "industry": "Restaurants", "pe_ratio": 35, "roe": 25},
    "CAVA":  {"market_cap": 7.5,  "fcf": 30,     "debt_to_equity": 0.1,  "sector": "Consumer Cyclical","revenue_growth": 35.1,  "industry": "Restaurants", "pe_ratio": 33, "roe": 12},
    "BOOT":  {"market_cap": 4.1,  "fcf": 110,    "debt_to_equity": 0.2,  "sector": "Consumer Cyclical","revenue_growth": 12.3,  "industry": "Apparel Retail", "pe_ratio": 18, "roe": 20},

    # --- Financials (Small-Mid Cap) ---
    "STEP":  {"market_cap": 5.5,  "fcf": 200,    "debt_to_equity": 0.9,  "sector": "Financial Services","revenue_growth": 22.4, "industry": "Asset Management", "pe_ratio": 14, "roe": 18},
    "LPLA":  {"market_cap": 9.8,  "fcf": 350,    "debt_to_equity": 1.8,  "sector": "Financial Services","revenue_growth": 16.5, "industry": "Capital Markets", "pe_ratio": 12, "roe": 15},
    "IBKR":  {"market_cap": 8.5,  "fcf": 900,    "debt_to_equity": 0.3,  "sector": "Financial Services","revenue_growth": 14.2, "industry": "Capital Markets", "pe_ratio": 10, "roe": 17},

    # --- Energy (Small-Mid Cap) ---
    "CTRA":  {"market_cap": 8.1,  "fcf": 650,    "debt_to_equity": 0.3,  "sector": "Energy",          "revenue_growth": -8.2,  "industry": "Oil & Gas E&P", "pe_ratio": 7, "roe": 12},
    "MTDR":  {"market_cap": 5.3,  "fcf": 400,    "debt_to_equity": 0.4,  "sector": "Energy",          "revenue_growth": 12.1,  "industry": "Oil & Gas E&P", "pe_ratio": 9, "roe": 18},

    # --- True Small-Cap ($0.3-2B) ---
    "CRS":   {"market_cap": 1.9,  "fcf": 85,     "debt_to_equity": 0.4,  "sector": "Industrials",     "revenue_growth": 18.3,  "industry": "Steel", "pe_ratio": 16, "roe": 15},
    "KALU":  {"market_cap": 1.2,  "fcf": 60,     "debt_to_equity": 0.8,  "sector": "Industrials",     "revenue_growth": 8.5,   "industry": "Metal Fabrication", "pe_ratio": 14, "roe": 10},
    "HAYW":  {"market_cap": 1.4,  "fcf": 70,     "debt_to_equity": 1.9,  "sector": "Industrials",     "revenue_growth": 5.2,   "industry": "Specialty Industrial Machinery", "pe_ratio": 18, "roe": 12},
    "XPEL":  {"market_cap": 1.1,  "fcf": 40,     "debt_to_equity": 0.1,  "sector": "Consumer Cyclical","revenue_growth": 12.8,  "industry": "Auto Parts", "pe_ratio": 22, "roe": 18},
    "RELY":  {"market_cap": 0.9,  "fcf": 25,     "debt_to_equity": 0.5,  "sector": "Technology",      "revenue_growth": 28.4,  "industry": "Software", "pe_ratio": 36, "roe": 16},
    "VERX":  {"market_cap": 1.6,  "fcf": 35,     "debt_to_equity": 0.3,  "sector": "Technology",      "revenue_growth": 22.1,  "industry": "Software", "pe_ratio": 34, "roe": 19},
    "SPSC":  {"market_cap": 1.8,  "fcf": 55,     "debt_to_equity": 0.2,  "sector": "Technology",      "revenue_growth": 15.6,  "industry": "Software", "pe_ratio": 30, "roe": 22},
    "CARG":  {"market_cap": 1.3,  "fcf": 90,     "debt_to_equity": 0.0,  "sector": "Technology",      "revenue_growth": 10.4,  "industry": "Internet Content", "pe_ratio": 24, "roe": 25},
    "TMDX":  {"market_cap": 1.7,  "fcf": -15,    "debt_to_equity": 0.6,  "sector": "Healthcare",      "revenue_growth": 45.2,  "industry": "Medical Devices", "pe_ratio": 30, "roe": 10},
    "KRYS":  {"market_cap": 1.5,  "fcf": 20,     "debt_to_equity": 0.0,  "sector": "Healthcare",      "revenue_growth": 52.1,  "industry": "Biotechnology", "pe_ratio": 26, "roe": 15},
    "ACLX":  {"market_cap": 0.8,  "fcf": -40,    "debt_to_equity": 0.0,  "sector": "Healthcare",      "revenue_growth": 120.5, "industry": "Biotechnology", "pe_ratio": 20, "roe": 8},
    "VCNX":  {"market_cap": 0.4,  "fcf": -20,    "debt_to_equity": 0.1,  "sector": "Healthcare",      "revenue_growth": 35.8,  "industry": "Biotechnology", "pe_ratio": 18, "roe": 6},
    "CNXC":  {"market_cap": 1.9,  "fcf": 110,    "debt_to_equity": 0.7,  "sector": "Technology",      "revenue_growth": 6.8,   "industry": "IT Services", "pe_ratio": 20, "roe": 18},
    "PAYO":  {"market_cap": 0.6,  "fcf": 30,     "debt_to_equity": 0.4,  "sector": "Financial Services","revenue_growth": 18.2, "industry": "Credit Services", "pe_ratio": 13, "roe": 12},
    "PRCT":  {"market_cap": 1.1,  "fcf": -25,    "debt_to_equity": 0.3,  "sector": "Healthcare",      "revenue_growth": 30.7,  "industry": "Medical Devices", "pe_ratio": 24, "roe": 9},
    "CALX":  {"market_cap": 1.0,  "fcf": 45,     "debt_to_equity": 0.0,  "sector": "Technology",      "revenue_growth": -5.3,  "industry": "Communication Equipment", "pe_ratio": 22, "roe": 20},
    "DOCS":  {"market_cap": 1.8,  "fcf": 60,     "debt_to_equity": 0.1,  "sector": "Healthcare",      "revenue_growth": 20.3,  "industry": "Health Information Services", "pe_ratio": 23, "roe": 16},
    "ITCI":  {"market_cap": 1.6,  "fcf": 50,     "debt_to_equity": 0.0,  "sector": "Healthcare",      "revenue_growth": 35.0,  "industry": "Biotechnology", "pe_ratio": 28, "roe": 22},
    "ELF":   {"market_cap": 1.4,  "fcf": 65,     "debt_to_equity": 0.3,  "sector": "Consumer Defensive","revenue_growth": 48.1, "industry": "Household & Personal Products", "pe_ratio": 25, "roe": 24},
    "LNTH":  {"market_cap": 1.7,  "fcf": 80,     "debt_to_equity": 1.2,  "sector": "Healthcare",      "revenue_growth": 25.6,  "industry": "Drug Manufacturers", "pe_ratio": 19, "roe": 18},

    # --- Large-Cap Missing (from _COMPANY_NAMES) ---
    "V":     {"market_cap": 550,  "fcf": 18000,  "debt_to_equity": 1.73, "sector": "Financial Services","revenue_growth": 10.2,  "industry": "Credit Services", "pe_ratio": 30, "roe": 45},
    "MA":    {"market_cap": 420,  "fcf": 12000,  "debt_to_equity": 5.20, "sector": "Financial Services","revenue_growth": 12.5,  "industry": "Credit Services", "pe_ratio": 35, "roe": 180},
    "PG":    {"market_cap": 380,  "fcf": 16000,  "debt_to_equity": 0.73, "sector": "Consumer Defensive","revenue_growth": 2.8,   "industry": "Household & Personal Products", "pe_ratio": 26, "roe": 30},
    "MRK":   {"market_cap": 260,  "fcf": 13000,  "debt_to_equity": 0.93, "sector": "Healthcare",       "revenue_growth": -1.2,  "industry": "Drug Manufacturers", "pe_ratio": 14, "roe": 35},
    "ABBV":  {"market_cap": 310,  "fcf": 22000,  "debt_to_equity": 5.80, "sector": "Healthcare",       "revenue_growth": 4.1,   "industry": "Drug Manufacturers", "pe_ratio": 18, "roe": 65},
    "KO":    {"market_cap": 270,  "fcf": 9500,   "debt_to_equity": 1.72, "sector": "Consumer Defensive","revenue_growth": 3.2,   "industry": "Beverages", "pe_ratio": 24, "roe": 40},
    "PEP":   {"market_cap": 230,  "fcf": 7200,   "debt_to_equity": 2.38, "sector": "Consumer Defensive","revenue_growth": 2.3,   "industry": "Beverages", "pe_ratio": 22, "roe": 50},
    "LLY":   {"market_cap": 700,  "fcf": 5800,   "debt_to_equity": 2.10, "sector": "Healthcare",       "revenue_growth": 32.0,  "industry": "Drug Manufacturers", "pe_ratio": 60, "roe": 55},
    "TMO":   {"market_cap": 200,  "fcf": 7500,   "debt_to_equity": 0.75, "sector": "Healthcare",       "revenue_growth": 1.4,   "industry": "Medical Instruments", "pe_ratio": 30, "roe": 15},
    "ABT":   {"market_cap": 190,  "fcf": 7000,   "debt_to_equity": 0.46, "sector": "Healthcare",       "revenue_growth": 2.5,   "industry": "Medical Devices", "pe_ratio": 25, "roe": 16},
    "DHR":   {"market_cap": 180,  "fcf": 6800,   "debt_to_equity": 0.35, "sector": "Healthcare",       "revenue_growth": -3.2,  "industry": "Medical Instruments", "pe_ratio": 28, "roe": 10},
    "LIN":   {"market_cap": 220,  "fcf": 6500,   "debt_to_equity": 0.32, "sector": "Basic Materials",  "revenue_growth": 2.1,   "industry": "Industrial Gases", "pe_ratio": 32, "roe": 12},
    "ACN":   {"market_cap": 210,  "fcf": 8200,   "debt_to_equity": 0.18, "sector": "Technology",       "revenue_growth": 3.8,   "industry": "IT Services", "pe_ratio": 28, "roe": 30},
    "CSCO":  {"market_cap": 220,  "fcf": 15000,  "debt_to_equity": 0.24, "sector": "Technology",       "revenue_growth": -5.7,  "industry": "Communication Equipment", "pe_ratio": 14, "roe": 28},
    "GS":    {"market_cap": 165,  "fcf": 8000,   "debt_to_equity": 2.58, "sector": "Financial Services","revenue_growth": 12.3,  "industry": "Capital Markets", "pe_ratio": 16, "roe": 12},
    "MS":    {"market_cap": 160,  "fcf": 7500,   "debt_to_equity": 2.81, "sector": "Financial Services","revenue_growth": 5.8,   "industry": "Capital Markets", "pe_ratio": 14, "roe": 13},
    "CAT":   {"market_cap": 170,  "fcf": 9500,   "debt_to_equity": 2.04, "sector": "Industrials",      "revenue_growth": 3.1,   "industry": "Farm & Heavy Construction Machinery", "pe_ratio": 16, "roe": 55},
    "GE":    {"market_cap": 190,  "fcf": 5800,   "debt_to_equity": 1.08, "sector": "Industrials",      "revenue_growth": 16.3,  "industry": "Aerospace & Defense", "pe_ratio": 30, "roe": 25},
    "HON":   {"market_cap": 135,  "fcf": 5200,   "debt_to_equity": 1.21, "sector": "Industrials",      "revenue_growth": 3.4,   "industry": "Diversified Industrials", "pe_ratio": 20, "roe": 32},
    "BA":    {"market_cap": 130,  "fcf": -4200,  "debt_to_equity": -6.5, "sector": "Industrials",      "revenue_growth": 16.8,  "industry": "Aerospace & Defense", "pe_ratio": 0, "roe": -40},
    "IBM":   {"market_cap": 195,  "fcf": 11500,  "debt_to_equity": 2.51, "sector": "Technology",       "revenue_growth": 3.5,   "industry": "IT Services", "pe_ratio": 22, "roe": 35},
    "LOW":   {"market_cap": 150,  "fcf": 7800,   "debt_to_equity": -15.0,"sector": "Consumer Cyclical", "revenue_growth": -2.1,  "industry": "Home Improvement Retail", "pe_ratio": 18, "roe": -200},
    "SBUX":  {"market_cap": 105,  "fcf": 3600,   "debt_to_equity": -7.0, "sector": "Consumer Cyclical", "revenue_growth": 1.8,   "industry": "Restaurants", "pe_ratio": 25, "roe": -150},
    "DE":    {"market_cap": 115,  "fcf": 5200,   "debt_to_equity": 2.15, "sector": "Industrials",      "revenue_growth": -15.2, "industry": "Farm & Heavy Construction Machinery", "pe_ratio": 14, "roe": 30},
    "RTX":   {"market_cap": 150,  "fcf": 5000,   "debt_to_equity": 0.58, "sector": "Industrials",      "revenue_growth": 8.4,   "industry": "Aerospace & Defense", "pe_ratio": 22, "roe": 10},
    "MMM":   {"market_cap": 70,   "fcf": 4200,   "debt_to_equity": 2.80, "sector": "Industrials",      "revenue_growth": -3.8,  "industry": "Diversified Industrials", "pe_ratio": 11, "roe": 30},
    "F":     {"market_cap": 42,   "fcf": 3500,   "debt_to_equity": 3.50, "sector": "Consumer Cyclical", "revenue_growth": 5.2,   "industry": "Auto Manufacturers", "pe_ratio": 6, "roe": 12},
    "GM":    {"market_cap": 48,   "fcf": 9200,   "debt_to_equity": 1.68, "sector": "Consumer Cyclical", "revenue_growth": 9.7,   "industry": "Auto Manufacturers", "pe_ratio": 5, "roe": 15},

    # --- Utilities ---
    "NEE":   {"market_cap": 155,  "fcf": 2000,   "debt_to_equity": 1.35, "sector": "Utilities",        "revenue_growth": 14.5,  "industry": "Utilities - Renewables", "pe_ratio": 22, "roe": 12, "dividend_yield": 2.8, "beta": 0.55},
    "DUK":   {"market_cap": 88,   "fcf": 1200,   "debt_to_equity": 1.42, "sector": "Utilities",        "revenue_growth": 5.1,   "industry": "Utilities - Regulated Electric", "pe_ratio": 18, "roe": 9, "dividend_yield": 3.8, "beta": 0.45},
    "SO":    {"market_cap": 92,   "fcf": 2800,   "debt_to_equity": 1.55, "sector": "Utilities",        "revenue_growth": 3.2,   "industry": "Utilities - Regulated Electric", "pe_ratio": 20, "roe": 12, "dividend_yield": 3.5, "beta": 0.50},
    "AEP":   {"market_cap": 52,   "fcf": 800,    "debt_to_equity": 1.61, "sector": "Utilities",        "revenue_growth": 4.8,   "industry": "Utilities - Regulated Electric", "pe_ratio": 17, "roe": 10, "dividend_yield": 3.7, "beta": 0.48},
    "D":     {"market_cap": 48,   "fcf": 1100,   "debt_to_equity": 1.38, "sector": "Utilities",        "revenue_growth": -2.5,  "industry": "Utilities - Regulated Electric", "pe_ratio": 15, "roe": 8, "dividend_yield": 4.8, "beta": 0.55},
    "SRE":   {"market_cap": 55,   "fcf": 900,    "debt_to_equity": 1.20, "sector": "Utilities",        "revenue_growth": 8.3,   "industry": "Utilities - Diversified", "pe_ratio": 18, "roe": 11, "dividend_yield": 2.9, "beta": 0.60},

    # --- Real Estate (REITs) ---
    "PLD":   {"market_cap": 110,  "fcf": 3500,   "debt_to_equity": 0.40, "sector": "Real Estate",      "revenue_growth": 8.5,   "industry": "Industrial REITs", "pe_ratio": 40, "roe": 8, "dividend_yield": 3.2},
    "AMT":   {"market_cap": 95,   "fcf": 2800,   "debt_to_equity": 3.40, "sector": "Real Estate",      "revenue_growth": 4.2,   "industry": "Telecom Tower REITs", "pe_ratio": 45, "roe": 18, "dividend_yield": 3.0},
    "EQIX":  {"market_cap": 75,   "fcf": 2200,   "debt_to_equity": 1.15, "sector": "Real Estate",      "revenue_growth": 9.8,   "industry": "Data Center REITs", "pe_ratio": 80, "roe": 6, "dividend_yield": 2.1},
    "O":     {"market_cap": 50,   "fcf": 1500,   "debt_to_equity": 0.55, "sector": "Real Estate",      "revenue_growth": 18.5,  "industry": "Retail REITs", "pe_ratio": 55, "roe": 3, "dividend_yield": 5.4},
    "SPG":   {"market_cap": 55,   "fcf": 2000,   "debt_to_equity": 8.50, "sector": "Real Estate",      "revenue_growth": 5.1,   "industry": "Retail REITs", "pe_ratio": 20, "roe": 45, "dividend_yield": 5.0},

    # --- Basic Materials ---
    "APD":   {"market_cap": 65,   "fcf": 2500,   "debt_to_equity": 0.52, "sector": "Basic Materials",  "revenue_growth": -3.1,  "industry": "Industrial Gases", "pe_ratio": 25, "roe": 14},
    "ECL":   {"market_cap": 60,   "fcf": 1800,   "debt_to_equity": 0.83, "sector": "Basic Materials",  "revenue_growth": 5.2,   "industry": "Specialty Chemicals", "pe_ratio": 35, "roe": 20},
    "SHW":   {"market_cap": 85,   "fcf": 2600,   "debt_to_equity": 2.75, "sector": "Basic Materials",  "revenue_growth": 3.8,   "industry": "Specialty Chemicals", "pe_ratio": 30, "roe": 65},
    "NUE":   {"market_cap": 32,   "fcf": 2200,   "debt_to_equity": 0.30, "sector": "Basic Materials",  "revenue_growth": -18.5, "industry": "Steel", "pe_ratio": 8, "roe": 18},
    "FCX":   {"market_cap": 58,   "fcf": 3500,   "debt_to_equity": 0.54, "sector": "Basic Materials",  "revenue_growth": 15.2,  "industry": "Copper", "pe_ratio": 20, "roe": 22},
    "NEM":   {"market_cap": 50,   "fcf": 2800,   "debt_to_equity": 0.35, "sector": "Basic Materials",  "revenue_growth": 30.1,  "industry": "Gold", "pe_ratio": 15, "roe": 12},
    "DOW":   {"market_cap": 28,   "fcf": 1200,   "debt_to_equity": 1.42, "sector": "Basic Materials",  "revenue_growth": -8.3,  "industry": "Chemicals", "pe_ratio": 22, "roe": 10, "dividend_yield": 5.2},

    # --- Telecom ---
    "T":     {"market_cap": 150,  "fcf": 16000,  "debt_to_equity": 1.23, "sector": "Communication Services","revenue_growth": 0.8, "industry": "Telecom Services", "pe_ratio": 10, "roe": 12, "dividend_yield": 5.1, "beta": 0.65},
    "VZ":    {"market_cap": 175,  "fcf": 18000,  "debt_to_equity": 1.58, "sector": "Communication Services","revenue_growth": 1.5, "industry": "Telecom Services", "pe_ratio": 9, "roe": 22, "dividend_yield": 6.3, "beta": 0.40},
    "TMUS":  {"market_cap": 260,  "fcf": 17000,  "debt_to_equity": 1.35, "sector": "Communication Services","revenue_growth": 3.2, "industry": "Telecom Services", "pe_ratio": 25, "roe": 15, "beta": 0.55},

    # --- Aerospace / Defense ---
    "LMT":   {"market_cap": 120,  "fcf": 6200,   "debt_to_equity": 2.85, "sector": "Industrials",      "revenue_growth": 5.8,   "industry": "Aerospace & Defense", "pe_ratio": 18, "roe": 85, "dividend_yield": 2.5},
    "NOC":   {"market_cap": 75,   "fcf": 2500,   "debt_to_equity": 1.60, "sector": "Industrials",      "revenue_growth": 4.2,   "industry": "Aerospace & Defense", "pe_ratio": 20, "roe": 30, "dividend_yield": 1.5},
    "GD":    {"market_cap": 72,   "fcf": 3200,   "debt_to_equity": 0.75, "sector": "Industrials",      "revenue_growth": 10.5,  "industry": "Aerospace & Defense", "pe_ratio": 19, "roe": 18, "dividend_yield": 2.0},
    "LHX":   {"market_cap": 42,   "fcf": 2100,   "debt_to_equity": 0.68, "sector": "Industrials",      "revenue_growth": 7.2,   "industry": "Aerospace & Defense", "pe_ratio": 16, "roe": 12},
    "HII":   {"market_cap": 9.5,  "fcf": 450,    "debt_to_equity": 1.80, "sector": "Industrials",      "revenue_growth": 8.1,   "industry": "Aerospace & Defense", "pe_ratio": 14, "roe": 22},

    # --- Transportation / Logistics ---
    "UPS":   {"market_cap": 105,  "fcf": 5200,   "debt_to_equity": 2.10, "sector": "Industrials",      "revenue_growth": -9.3,  "industry": "Integrated Freight & Logistics", "pe_ratio": 16, "roe": 35, "dividend_yield": 4.5},
    "FDX":   {"market_cap": 65,   "fcf": 3800,   "debt_to_equity": 0.82, "sector": "Industrials",      "revenue_growth": -2.8,  "industry": "Integrated Freight & Logistics", "pe_ratio": 14, "roe": 16},
    "DAL":   {"market_cap": 32,   "fcf": 3200,   "debt_to_equity": 2.80, "sector": "Industrials",      "revenue_growth": 5.4,   "industry": "Airlines", "pe_ratio": 7, "roe": 45},
    "UAL":   {"market_cap": 24,   "fcf": 3500,   "debt_to_equity": 3.10, "sector": "Industrials",      "revenue_growth": 7.1,   "industry": "Airlines", "pe_ratio": 6, "roe": 50},
    "ODFL":  {"market_cap": 38,   "fcf": 1000,   "debt_to_equity": 0.04, "sector": "Industrials",      "revenue_growth": 3.2,   "industry": "Trucking", "pe_ratio": 32, "roe": 28},

    # --- Energy (More coverage) ---
    "COP":   {"market_cap": 130,  "fcf": 10000,  "debt_to_equity": 0.38, "sector": "Energy",           "revenue_growth": -8.5,  "industry": "Oil & Gas E&P", "pe_ratio": 11, "roe": 20, "dividend_yield": 3.0},
    "SLB":   {"market_cap": 60,   "fcf": 3800,   "debt_to_equity": 0.58, "sector": "Energy",           "revenue_growth": 12.8,  "industry": "Oil & Gas Equipment", "pe_ratio": 14, "roe": 22},
    "EOG":   {"market_cap": 65,   "fcf": 5200,   "debt_to_equity": 0.17, "sector": "Energy",           "revenue_growth": -5.1,  "industry": "Oil & Gas E&P", "pe_ratio": 9, "roe": 25, "dividend_yield": 2.8},
    "OKE":   {"market_cap": 55,   "fcf": 2800,   "debt_to_equity": 1.65, "sector": "Energy",           "revenue_growth": 15.3,  "industry": "Oil & Gas Midstream", "pe_ratio": 17, "roe": 30, "dividend_yield": 4.2},
    "FANG":  {"market_cap": 45,   "fcf": 3800,   "debt_to_equity": 0.35, "sector": "Energy",           "revenue_growth": 22.5,  "industry": "Oil & Gas E&P", "pe_ratio": 8, "roe": 28, "dividend_yield": 2.0},

    # --- Consumer / Retail (More coverage) ---
    "TGT":   {"market_cap": 62,   "fcf": 4200,   "debt_to_equity": 1.35, "sector": "Consumer Defensive","revenue_growth": -4.2,  "industry": "Discount Stores", "pe_ratio": 13, "roe": 30, "dividend_yield": 3.5},
    "ROST":  {"market_cap": 48,   "fcf": 2500,   "debt_to_equity": 2.10, "sector": "Consumer Cyclical", "revenue_growth": 7.2,   "industry": "Apparel Retail", "pe_ratio": 22, "roe": 45},
    "TJX":   {"market_cap": 120,  "fcf": 4800,   "debt_to_equity": 4.50, "sector": "Consumer Cyclical", "revenue_growth": 8.5,   "industry": "Apparel Retail", "pe_ratio": 26, "roe": 65},
    "LULU":  {"market_cap": 42,   "fcf": 1800,   "debt_to_equity": 0.45, "sector": "Consumer Cyclical", "revenue_growth": 18.2,  "industry": "Apparel Retail", "pe_ratio": 28, "roe": 42},
    "DG":    {"market_cap": 18,   "fcf": 500,    "debt_to_equity": 3.20, "sector": "Consumer Defensive","revenue_growth": 2.1,   "industry": "Discount Stores", "pe_ratio": 12, "roe": 22},
    "YUM":   {"market_cap": 38,   "fcf": 1600,   "debt_to_equity": -8.0, "sector": "Consumer Cyclical", "revenue_growth": 6.8,   "industry": "Restaurants", "pe_ratio": 25, "roe": -120, "dividend_yield": 1.9},
    "CMG":   {"market_cap": 78,   "fcf": 1200,   "debt_to_equity": 3.50, "sector": "Consumer Cyclical", "revenue_growth": 14.3,  "industry": "Restaurants", "pe_ratio": 55, "roe": -150},
    "DECK":  {"market_cap": 22,   "fcf": 800,    "debt_to_equity": 0.05, "sector": "Consumer Cyclical", "revenue_growth": 18.2,  "industry": "Footwear & Accessories", "pe_ratio": 25, "roe": 35},

    # --- Insurance ---
    "BRK.B": {"market_cap": 850,  "fcf": 32000,  "debt_to_equity": 0.25, "sector": "Financial Services","revenue_growth": 8.2,   "industry": "Insurance - Diversified", "pe_ratio": 10, "roe": 15},
    "PGR":   {"market_cap": 120,  "fcf": 8500,   "debt_to_equity": 0.85, "sector": "Financial Services","revenue_growth": 25.3,  "industry": "Insurance - Property & Casualty", "pe_ratio": 18, "roe": 30},
    "CB":    {"market_cap": 105,  "fcf": 6200,   "debt_to_equity": 0.28, "sector": "Financial Services","revenue_growth": 12.1,  "industry": "Insurance - Property & Casualty", "pe_ratio": 12, "roe": 16},
    "MET":   {"market_cap": 52,   "fcf": 4500,   "debt_to_equity": 0.50, "sector": "Financial Services","revenue_growth": 5.8,   "industry": "Insurance - Life", "pe_ratio": 8, "roe": 12, "dividend_yield": 2.8},

    # --- Pharmaceuticals / Biotech (More) ---
    "GILD":  {"market_cap": 105,  "fcf": 8000,   "debt_to_equity": 1.35, "sector": "Healthcare",       "revenue_growth": 5.5,   "industry": "Biotechnology", "pe_ratio": 12, "roe": 30, "dividend_yield": 3.2},
    "AMGN":  {"market_cap": 160,  "fcf": 9500,   "debt_to_equity": 8.50, "sector": "Healthcare",       "revenue_growth": 20.8,  "industry": "Biotechnology", "pe_ratio": 22, "roe": -150, "dividend_yield": 3.0},
    "REGN":  {"market_cap": 85,   "fcf": 4500,   "debt_to_equity": 0.08, "sector": "Healthcare",       "revenue_growth": 6.3,   "industry": "Biotechnology", "pe_ratio": 18, "roe": 20},
    "VRTX":  {"market_cap": 110,  "fcf": 4200,   "debt_to_equity": 0.05, "sector": "Healthcare",       "revenue_growth": 14.2,  "industry": "Biotechnology", "pe_ratio": 28, "roe": 25},
    "BMY":   {"market_cap": 95,   "fcf": 12000,  "debt_to_equity": 3.15, "sector": "Healthcare",       "revenue_growth": -2.8,  "industry": "Drug Manufacturers", "pe_ratio": 8, "roe": 45, "dividend_yield": 4.5},

    # --- Small-Cap Industrials (more manufacturing) ---
    "TRS":   {"market_cap": 0.7,  "fcf": 30,     "debt_to_equity": 0.3,  "sector": "Industrials",      "revenue_growth": 8.5,   "industry": "Specialty Industrial Machinery", "pe_ratio": 12, "roe": 15},
    "WIRE":  {"market_cap": 1.5,  "fcf": 120,    "debt_to_equity": 0.0,  "sector": "Industrials",      "revenue_growth": -12.5, "industry": "Electrical Equipment", "pe_ratio": 10, "roe": 22},
    "POWL":  {"market_cap": 1.8,  "fcf": 90,     "debt_to_equity": 0.0,  "sector": "Industrials",      "revenue_growth": 45.2,  "industry": "Electrical Equipment", "pe_ratio": 15, "roe": 30},
    "PRIM":  {"market_cap": 1.6,  "fcf": 70,     "debt_to_equity": 0.5,  "sector": "Industrials",      "revenue_growth": 22.3,  "industry": "Engineering & Construction", "pe_ratio": 14, "roe": 18},
    "GBX":   {"market_cap": 1.3,  "fcf": 60,     "debt_to_equity": 0.7,  "sector": "Industrials",      "revenue_growth": 15.1,  "industry": "Railroads", "pe_ratio": 11, "roe": 16},
    "NN":    {"market_cap": 0.5,  "fcf": 20,     "debt_to_equity": 1.2,  "sector": "Industrials",      "revenue_growth": 5.8,   "industry": "Specialty Industrial Machinery", "pe_ratio": 18, "roe": 8},
    "ROCK":  {"market_cap": 0.8,  "fcf": 35,     "debt_to_equity": 0.2,  "sector": "Industrials",      "revenue_growth": 10.2,  "industry": "Specialty Industrial Machinery", "pe_ratio": 13, "roe": 14},

    # --- Small-Cap Consumer ---
    "DNUT":  {"market_cap": 1.2,  "fcf": 30,     "debt_to_equity": 1.8,  "sector": "Consumer Defensive","revenue_growth": 12.5,  "industry": "Packaged Foods", "pe_ratio": 35, "roe": 8},
    "ARKO":  {"market_cap": 0.5,  "fcf": 45,     "debt_to_equity": 2.1,  "sector": "Consumer Defensive","revenue_growth": -5.2,  "industry": "Food Distribution", "pe_ratio": 8, "roe": 6},
    "ONEW":  {"market_cap": 0.6,  "fcf": 55,     "debt_to_equity": 0.8,  "sector": "Consumer Cyclical", "revenue_growth": -8.3,  "industry": "Leisure", "pe_ratio": 4, "roe": 12},
    "CURV":  {"market_cap": 0.4,  "fcf": 15,     "debt_to_equity": 1.5,  "sector": "Consumer Cyclical", "revenue_growth": 8.1,   "industry": "Apparel Retail", "pe_ratio": 20, "roe": 10},

    # --- Small-Cap Tech ---
    "INTA":  {"market_cap": 1.2,  "fcf": 40,     "debt_to_equity": 0.3,  "sector": "Technology",       "revenue_growth": 28.5,  "industry": "Software", "pe_ratio": 32, "roe": 18},
    "ALKT":  {"market_cap": 0.9,  "fcf": 25,     "debt_to_equity": 0.4,  "sector": "Technology",       "revenue_growth": 15.3,  "industry": "Software", "pe_ratio": 28, "roe": 14},
    "SMAR":  {"market_cap": 1.4,  "fcf": 55,     "debt_to_equity": 0.2,  "sector": "Technology",       "revenue_growth": 18.7,  "industry": "Software", "pe_ratio": 50, "roe": 8},
    "RAMP":  {"market_cap": 0.6,  "fcf": 20,     "debt_to_equity": 0.1,  "sector": "Technology",       "revenue_growth": 22.1,  "industry": "Software", "pe_ratio": 25, "roe": 12},

    # --- Small-Cap Energy ---
    "VTLE":  {"market_cap": 1.0,  "fcf": 250,    "debt_to_equity": 0.4,  "sector": "Energy",           "revenue_growth": -15.2, "industry": "Oil & Gas E&P", "pe_ratio": 4, "roe": 18},
    "CHRD":  {"market_cap": 6.5,  "fcf": 1800,   "debt_to_equity": 0.3,  "sector": "Energy",           "revenue_growth": 35.2,  "industry": "Oil & Gas E&P", "pe_ratio": 5, "roe": 22, "dividend_yield": 8.5},
    "SM":    {"market_cap": 4.2,  "fcf": 700,    "debt_to_equity": 0.4,  "sector": "Energy",           "revenue_growth": 12.8,  "industry": "Oil & Gas E&P", "pe_ratio": 6, "roe": 20},
}

# Common ticker -> company name map for Wikipedia scraping
_COMPANY_NAMES: dict[str, str] = {
    "AAPL": "Apple Inc.", "MSFT": "Microsoft", "GOOGL": "Alphabet Inc.",
    "AMZN": "Amazon (company)", "META": "Meta Platforms", "NVDA": "Nvidia",
    "TSLA": "Tesla, Inc.", "BRK.B": "Berkshire Hathaway", "JPM": "JPMorgan Chase",
    "V": "Visa Inc.", "JNJ": "Johnson & Johnson", "WMT": "Walmart",
    "PG": "Procter & Gamble", "MA": "Mastercard", "HD": "The Home Depot",
    "CVX": "Chevron Corporation", "MRK": "Merck & Co.", "ABBV": "AbbVie",
    "KO": "The Coca-Cola Company", "PEP": "PepsiCo", "AVGO": "Broadcom Inc.",
    "COST": "Costco", "TMO": "Thermo Fisher Scientific", "MCD": "McDonald's",
    "ACN": "Accenture", "CSCO": "Cisco", "ABT": "Abbott Laboratories",
    "DHR": "Danaher Corporation", "LIN": "Linde plc", "ADBE": "Adobe Inc.",
    "CRM": "Salesforce", "NKE": "Nike, Inc.", "TXN": "Texas Instruments",
    "UNH": "UnitedHealth Group", "NFLX": "Netflix", "AMD": "Advanced Micro Devices",
    "INTC": "Intel", "DIS": "The Walt Disney Company", "BA": "Boeing",
    "GS": "Goldman Sachs", "MS": "Morgan Stanley", "CAT": "Caterpillar Inc.",
    "GE": "General Electric", "IBM": "IBM", "QCOM": "Qualcomm",
    "HON": "Honeywell", "UPS": "United Parcel Service", "LOW": "Lowe's",
    "SBUX": "Starbucks", "PFE": "Pfizer", "LLY": "Eli Lilly and Company",
    "XOM": "ExxonMobil", "COP": "ConocoPhillips", "SLB": "Schlumberger",
    "RTX": "RTX Corporation", "DE": "John Deere", "MMM": "3M",
    "PYPL": "PayPal", "SQ": "Block, Inc.", "SHOP": "Shopify",
    "SNOW": "Snowflake Inc.", "NET": "Cloudflare", "CRWD": "CrowdStrike",
    "ZS": "Zscaler", "DDOG": "Datadog", "MDB": "MongoDB",
    "PLTR": "Palantir Technologies", "UBER": "Uber", "ABNB": "Airbnb",
    "RIVN": "Rivian", "LCID": "Lucid Motors", "F": "Ford Motor Company",
    "GM": "General Motors", "NEE": "NextEra Energy", "DUK": "Duke Energy",
    "SO": "Southern Company", "PLD": "Prologis", "AMT": "American Tower",
    # Manufacturing / Industrials
    "RBC": "Roper Technologies", "GGG": "Graco Inc.", "AOS": "A. O. Smith",
    "GNRC": "Generac", "MIDD": "Middleby Corporation", "WTS": "Watts Water Technologies",
    "MLI": "Mueller Industries", "ASTE": "Astec Industries", "NPO": "EnPro Industries",
    "EPAC": "Enerpac Tool Group", "SWK": "Stanley Black & Decker",
    "EMR": "Emerson Electric", "ROK": "Rockwell Automation",
    "DOV": "Dover Corporation", "ITW": "Illinois Tool Works",
    # Healthcare small-mid
    "GMED": "Globus Medical", "IART": "Integra LifeSciences", "NVCR": "NovoCure",
    "HALO": "Halozyme Therapeutics",
    # Tech small-mid
    "FOUR": "Shift4 Payments", "PCTY": "Paylocity", "CWAN": "Clearwater Analytics",
    "BRZE": "Braze (company)", "JAMF": "Jamf",
    # Consumer small-mid
    "SHAK": "Shake Shack", "WING": "Wingstop", "CAVA": "Cava Group",
    "BOOT": "Boot Barn",
    # Financials small-mid
    "STEP": "StepStone Group",
    # NIO
    "NIO": "NIO",
    # Large-cap additions
    "V": "Visa Inc.", "MA": "Mastercard", "PG": "Procter & Gamble",
    "MRK": "Merck & Co.", "ABBV": "AbbVie", "KO": "The Coca-Cola Company",
    "PEP": "PepsiCo", "LLY": "Eli Lilly and Company", "TMO": "Thermo Fisher Scientific",
    "ABT": "Abbott Laboratories", "DHR": "Danaher Corporation", "LIN": "Linde plc",
    "ACN": "Accenture", "CSCO": "Cisco", "GS": "Goldman Sachs",
    "MS": "Morgan Stanley", "CAT": "Caterpillar Inc.", "GE": "GE Aerospace",
    "HON": "Honeywell", "BA": "Boeing", "IBM": "IBM",
    "LOW": "Lowe's", "SBUX": "Starbucks", "DE": "John Deere",
    "RTX": "RTX Corporation", "MMM": "3M", "F": "Ford Motor Company",
    "GM": "General Motors",
    # Utilities
    "NEE": "NextEra Energy", "DUK": "Duke Energy", "SO": "Southern Company",
    "AEP": "American Electric Power", "D": "Dominion Energy", "SRE": "Sempra Energy",
    # Real Estate
    "PLD": "Prologis", "AMT": "American Tower", "EQIX": "Equinix",
    "O": "Realty Income", "SPG": "Simon Property Group",
    # Materials
    "APD": "Air Products and Chemicals", "ECL": "Ecolab", "SHW": "Sherwin-Williams",
    "NUE": "Nucor Corporation", "FCX": "Freeport-McMoRan", "NEM": "Newmont Corporation",
    "DOW": "Dow Inc.",
    # Telecom
    "T": "AT&T", "VZ": "Verizon Communications", "TMUS": "T-Mobile US",
    # Aerospace/Defense
    "LMT": "Lockheed Martin", "NOC": "Northrop Grumman", "GD": "General Dynamics",
    "LHX": "L3Harris Technologies", "HII": "Huntington Ingalls Industries",
    # Transport
    "UPS": "United Parcel Service", "FDX": "FedEx", "DAL": "Delta Air Lines",
    "UAL": "United Airlines", "ODFL": "Old Dominion Freight Line",
    # Energy
    "COP": "ConocoPhillips", "SLB": "Schlumberger", "EOG": "EOG Resources",
    "OKE": "ONEOK", "FANG": "Diamondback Energy",
    # Consumer/Retail
    "TGT": "Target Corporation", "ROST": "Ross Stores", "TJX": "TJX Companies",
    "LULU": "Lululemon Athletica", "DG": "Dollar General", "YUM": "Yum! Brands",
    "CMG": "Chipotle Mexican Grill", "DECK": "Deckers Outdoor",
    # Insurance
    "BRK.B": "Berkshire Hathaway", "PGR": "Progressive Corporation",
    "CB": "Chubb Limited", "MET": "MetLife",
    # Pharma/Biotech
    "GILD": "Gilead Sciences", "AMGN": "Amgen", "REGN": "Regeneron Pharmaceuticals",
    "VRTX": "Vertex Pharmaceuticals", "BMY": "Bristol-Myers Squibb",
    # Small-cap industrials
    "TRS": "TriMas Corporation", "WIRE": "Encore Wire", "POWL": "Powell Industries",
    "PRIM": "Primoris Services", "GBX": "Greenbrier Companies", "NN": "NextGen Healthcare",
    "ROCK": "Gibraltar Industries",
    # Small-cap consumer
    "DNUT": "Krispy Kreme", "ARKO": "Arko Corp", "ONEW": "OneWater Marine",
    "CURV": "Torrid Holdings",
    # Small-cap tech
    "INTA": "Intapp", "ALKT": "Alkami Technology", "SMAR": "Smartsheet",
    "RAMP": "LiveRamp",
    # Small-cap energy
    "VTLE": "Vital Energy", "CHRD": "Chord Energy", "SM": "SM Energy",
}

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ScreenRequest(BaseModel):
    """Incoming request body with a free-text screening prompt."""
    prompt: str = Field("", max_length=500, description="Natural-language stock screening prompt")
    rules: dict[str, Any] | None = Field(default=None, description="Structured screening rules that bypass LLM parsing")


class ScreenResponse(BaseModel):
    """Response containing matched tickers and the parsed rules."""
    tickers: list[str]
    rules: dict[str, Any]
    count: int
    universe_size: int = 9920
    unresolved: list[str] = []
    notes: str = ""


# ---------------------------------------------------------------------------
# Ollama LLM helper
# ---------------------------------------------------------------------------

import os

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama-agent:latest")

SYSTEM_PROMPT = """\
You are a stock-screening rule extractor.  Given a user's natural-language
request, output ONLY a JSON object (no markdown, no explanation) with any
combination of these optional fields:

  min_market_cap       — minimum market cap in billions (number)
  max_market_cap       — maximum market cap in billions (number)
  min_fcf              — minimum free-cash-flow in millions (number)
  max_debt_to_equity   — maximum debt-to-equity ratio (number)
  sectors              — list of sector names to include (list of strings).
                         Valid sectors: Technology, Financials,
                         Health Care, Energy, Consumer Discretionary,
                         Consumer Staples, Communication Services,
                         Industrials, Materials, Real Estate, Utilities
  industry             — specific industry name (string, e.g. 'Auto Manufacturers',
                         'Software', 'Steel', 'Manufacturing')
  min_revenue_growth   — minimum revenue growth percentage (number)
  min_dividend_yield   — minimum dividend yield percentage (number)
  max_pe_ratio         — maximum price-to-earnings ratio (number)
  min_pe_ratio         — minimum price-to-earnings ratio (number)
  max_beta             — maximum beta (number)
  min_beta             — minimum beta (number)
  positive_fcf         — true if company must have positive free cash flow (boolean)
  max_debt_to_equity   — already listed above
  min_roe              — minimum return on equity percentage (number)
  min_gross_margin     — minimum gross margin percentage (number)
  min_net_margin       — minimum net profit margin percentage (number)
  max_peg_ratio        — maximum PEG ratio (number, lower=cheaper relative to growth)
  max_ev_to_ebitda     — maximum EV/EBITDA ratio (number)
  min_current_ratio    — minimum current ratio (number, >1 means liquid)
  min_eps_growth       — minimum EPS growth percentage (number)
  min_fcf_yield        — minimum free cash flow yield percentage (number)
  profitable_only      — true if company must have positive earnings/net income (boolean)
  unresolved           — list of criteria from the user's request that CANNOT
                         be mapped to any of the above fields (list of strings).
                         Examples: "non-MBA CEO", "CEO under 40 years old",
                         "CEO going through divorce", "high ARR", "female leadership",
                         "ethical supply chain", "AI-first company".
                         ALWAYS include criteria you cannot quantify here.

Market cap guidance (CRITICAL — always set BOTH min and max for size queries):
  Small-cap: min_market_cap=0.3, max_market_cap=2
  Mid-cap: min_market_cap=2, max_market_cap=10
  Large-cap: min_market_cap=10
  Mega-cap: min_market_cap=200

Examples:
  "small-cap tech stocks" -> {"min_market_cap": 0.3, "max_market_cap": 2, "sectors": ["Technology"]}
  "mid-cap healthcare with low debt" -> {"min_market_cap": 2, "max_market_cap": 10, "sectors": ["Health Care"], "max_debt_to_equity": 0.5}
  "large-cap dividend payers" -> {"min_market_cap": 10, "min_dividend_yield": 2}
  "profitable high-margin companies" -> {"profitable_only": true, "min_gross_margin": 50, "min_net_margin": 15}
  "undervalued growth stocks" -> {"max_peg_ratio": 1.5, "min_revenue_growth": 15, "max_pe_ratio": 25}
  "cash-rich companies with strong ROE" -> {"min_roe": 20, "positive_fcf": true, "min_current_ratio": 1.5}
  "cheap industrials by EV/EBITDA" -> {"sectors": ["Industrials"], "max_ev_to_ebitda": 12}
  "high FCF yield with EPS growth" -> {"min_fcf_yield": 5, "min_eps_growth": 10}
  "companies with non MBA CEOs in tech" -> {"sectors": ["Technology"], "unresolved": ["non-MBA CEO"]}
  "high ARR to expenses ratio" -> {"unresolved": ["high ARR to expenses ratio"]}
  "small cap ceo under 40 in manufacturing" -> {"min_market_cap": 0.3, "max_market_cap": 2, "industry": "Manufacturing", "unresolved": ["CEO under 40 years old"]}
  "steel companies with PE under 15" -> {"industry": "Steel", "max_pe_ratio": 15}
  "low-beta utilities" -> {"sectors": ["Utilities"], "max_beta": 0.8}

Omit any field the user did not mention.  Always respond with valid JSON only.\
"""


def _parse_llm_json(raw_text: str) -> dict | None:
    """Extract first valid JSON object from LLM response text."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"```(?:json)?", "", cleaned).strip()

    json_candidates: list[str] = []
    brace_depth = 0
    start_idx = None
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if brace_depth == 0:
                start_idx = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0 and start_idx is not None:
                json_candidates.append(cleaned[start_idx : i + 1])
                start_idx = None

    if not json_candidates:
        json_candidates = [cleaned]

    for candidate in json_candidates:
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


async def _call_ollama(prompt: str) -> str | None:
    """Try Ollama local LLM. Returns raw text or None on failure."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(OLLAMA_URL, json=payload)
            resp.raise_for_status()
        return resp.json()["message"]["content"]
    except Exception as exc:
        logger.warning("Ollama failed: %s", exc)
        return None


async def _call_groq(prompt: str) -> str | None:
    """Try Groq cloud LLM. Returns raw text or None on failure."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                },
            )
            resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("Groq failed: %s", exc)
        return None


async def _call_gemini(prompt: str) -> str | None:
    """Try Gemini cloud LLM. Returns raw text or None on failure."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nUser query: {prompt}"}]}],
                    "generationConfig": {"temperature": 0.0},
                },
            )
            resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        logger.warning("Gemini failed: %s", exc)
        return None


def _regex_intent_parser(prompt: str) -> dict[str, Any] | None:
    """Fallback: extract screening rules from prompt using regex patterns.

    Handles common queries without needing any LLM provider.
    Returns None if the prompt is too complex to parse with regex.
    """
    lower = prompt.lower()
    rules: dict[str, Any] = {}

    # Sector detection
    sector_map = {
        "tech": "technology", "technology": "technology",
        "health": "health care", "healthcare": "health care", "pharma": "health care",
        "biotech": "health care",
        "financ": "financials", "bank": "financials",
        "energy": "energy", "oil": "energy", "gas": "energy",
        "consumer": "consumer discretionary", "retail": "consumer discretionary",
        "industrial": "industrials", "manufactur": "industrials",
        "real estate": "real estate", "reit": "real estate",
        "utilit": "utilities",
        "material": "materials", "mining": "materials",
        "communicat": "communication services", "media": "communication services",
        "telecom": "communication services",
    }
    for keyword, sector in sector_map.items():
        if keyword in lower:
            rules["sectors"] = [sector]
            break

    # Market cap
    if "large cap" in lower or "large-cap" in lower or "mega cap" in lower:
        rules["min_market_cap"] = 10
    elif "mid cap" in lower or "mid-cap" in lower or "midcap" in lower:
        rules["min_market_cap"] = 2
        rules["max_market_cap"] = 10
    elif "small cap" in lower or "small-cap" in lower or "smallcap" in lower:
        rules["max_market_cap"] = 2
    elif "micro cap" in lower or "penny" in lower:
        rules["max_market_cap"] = 0.3

    # Revenue/earnings growth
    if "high growth" in lower or "high revenue growth" in lower or "fast growing" in lower:
        rules["min_revenue_growth"] = 15
    elif "growth" in lower and "revenue" in lower:
        rules["min_revenue_growth"] = 10

    # Value metrics
    if "undervalued" in lower or "value stock" in lower or "cheap" in lower:
        rules["max_pe_ratio"] = 15
    if "low pe" in lower or "low p/e" in lower:
        rules["max_pe_ratio"] = 12

    # Dividend
    if "dividend" in lower or "income" in lower or "yield" in lower:
        rules["min_dividend_yield"] = 2.0

    # Debt
    if "low debt" in lower or "no debt" in lower or "debt free" in lower:
        rules["max_debt_to_equity"] = 0.5

    # Profitability
    if "profitable" in lower or "high margin" in lower:
        rules["min_profit_margin"] = 10

    # FCF
    if "free cash flow" in lower or "fcf" in lower or "cash flow" in lower:
        rules["min_fcf"] = 100

    # Beta / volatility
    if "volatile" in lower or "high beta" in lower:
        rules["min_beta"] = 1.5
    elif "stable" in lower or "low beta" in lower or "defensive" in lower:
        rules["max_beta"] = 0.8

    if rules:
        logger.info("Screener intent parsed via regex fallback: %s", rules)
        return rules
    return None


async def intent_parser(prompt: str) -> dict[str, Any]:
    """Extract structured screening rules from a natural-language prompt.

    Tries providers in order: Ollama (local) -> Groq -> Gemini -> regex fallback.
    """
    raw_text = None

    # Try each LLM provider in order
    for name, caller in [("ollama", _call_ollama), ("groq", _call_groq), ("gemini", _call_gemini)]:
        raw_text = await caller(prompt)
        if raw_text:
            logger.info("Screener intent parsed via %s", name)
            break

    if not raw_text:
        # Regex fallback — no LLM needed
        regex_rules = _regex_intent_parser(prompt)
        if regex_rules:
            return regex_rules
        raise HTTPException(
            status_code=502,
            detail="No LLM provider available and query too complex for regex fallback",
        )

    rules = _parse_llm_json(raw_text)
    if rules is None:
        logger.error("Could not parse LLM output as JSON: %s", raw_text[:300])
        raise HTTPException(
            status_code=422,
            detail=f"LLM returned invalid JSON: {raw_text[:300]}",
        )

    # Remove null values — treat them as "not specified" (same as omitted)
    if isinstance(rules, dict):
        rules = {k: v for k, v in rules.items() if v is not None}

    # Unwrap single-key wrapper objects (e.g. {"rules": {...}})
    if isinstance(rules, dict) and len(rules) == 1:
        inner = next(iter(rules.values()))
        if isinstance(inner, dict):
            rules = inner

    # Post-processing: enforce market cap ranges when LLM misses max_market_cap.
    # Local LLMs often set min_market_cap but forget max_market_cap for small/mid-cap.
    prompt_lower = prompt.lower()
    if "small" in prompt_lower and "cap" in prompt_lower:
        rules.setdefault("min_market_cap", 0.3)
        rules.setdefault("max_market_cap", 2)
    elif "mid" in prompt_lower and "cap" in prompt_lower:
        rules.setdefault("min_market_cap", 2)
        rules.setdefault("max_market_cap", 10)
    elif "micro" in prompt_lower and "cap" in prompt_lower:
        rules.setdefault("min_market_cap", 0.05)
        rules.setdefault("max_market_cap", 0.3)
    elif "large" in prompt_lower and "cap" in prompt_lower:
        rules.setdefault("min_market_cap", 10)
    elif "mega" in prompt_lower and "cap" in prompt_lower:
        rules.setdefault("min_market_cap", 200)

    return rules


# ---------------------------------------------------------------------------
# Sector name aliases — maps various names to a canonical lowercase form
# so the LLM output, mock universe, and GICS (yfinance) names all match.
# ---------------------------------------------------------------------------

_SECTOR_ALIASES: dict[str, str] = {
    # GICS (yfinance / SP500 cache) -> canonical
    "financials":               "financials",
    "health care":              "health care",
    "consumer discretionary":   "consumer discretionary",
    "consumer staples":         "consumer staples",
    "information technology":   "information technology",
    "communication services":   "communication services",
    "industrials":              "industrials",
    "materials":                "materials",
    "real estate":              "real estate",
    "utilities":                "utilities",
    "energy":                   "energy",
    "technology":               "technology",
    # Mock universe / LLM aliases -> canonical
    "financial services":       "financials",
    "healthcare":               "health care",
    "consumer cyclical":        "consumer discretionary",
    "consumer defensive":       "consumer staples",
    "manufacturing":            "industrials",
}


def _normalize_sector(sector: str) -> str:
    """Normalise a sector name to its canonical lowercase form."""
    lower = sector.lower().strip()
    return _SECTOR_ALIASES.get(lower, lower)


# ---------------------------------------------------------------------------
# Screener logic
# ---------------------------------------------------------------------------

def swarm_evaluator(rules: dict[str, Any], universe: dict[str, dict[str, Any]]) -> list[str]:
    """Filter a universe of stocks against the parsed screening rules.

    Each rule key maps to a comparison against the corresponding fundamental
    field on every stock in the universe.  A stock must pass ALL provided
    rules to be included.

    Supported rule keys:
        - ``min_market_cap``: stock market_cap >= value
        - ``max_market_cap``: stock market_cap <= value
        - ``min_fcf``: stock fcf >= value
        - ``max_debt_to_equity``: stock debt_to_equity <= value
        - ``sectors``: stock sector is in the given list (case-insensitive,
          with alias mapping between GICS and common names)
        - ``industry``: stock industry contains value (substring, case-insensitive)
        - ``min_revenue_growth``: stock revenue_growth >= value
        - ``max_pe_ratio``: stock pe_ratio <= value (skips stocks with no PE)
        - ``min_pe_ratio``: stock pe_ratio >= value (skips stocks with no PE)
        - ``min_dividend_yield``: stock dividend_yield >= value
        - ``max_beta``: stock beta <= value
        - ``min_beta``: stock beta >= value

    Args:
        rules: Dict of screening constraints (output of ``intent_parser``).
        universe: Mapping of ticker -> fundamentals dict.

    Returns:
        Sorted list of ticker strings that satisfy every rule.
    """
    matches: list[str] = []

    min_market_cap = rules.get("min_market_cap")
    max_market_cap = rules.get("max_market_cap")
    min_fcf = rules.get("min_fcf")
    max_debt_to_equity = rules.get("max_debt_to_equity")
    sectors_raw = rules.get("sectors")
    min_revenue_growth = rules.get("min_revenue_growth")

    # Normalise sector list using alias map for cross-universe compatibility
    sectors_canonical: list[str] | None = None
    if sectors_raw and isinstance(sectors_raw, list):
        sectors_canonical = [_normalize_sector(s) for s in sectors_raw]

    for ticker, data in universe.items():
        # --- market cap ---
        if min_market_cap is not None and data["market_cap"] < min_market_cap:
            continue
        if max_market_cap is not None and data["market_cap"] > max_market_cap:
            continue

        # --- free cash flow ---
        if min_fcf is not None and data["fcf"] < min_fcf:
            continue

        # --- leverage ---
        if max_debt_to_equity is not None and data["debt_to_equity"] > max_debt_to_equity:
            continue

        # --- sector (normalised via alias map) ---
        if sectors_canonical is not None:
            stock_sector = _normalize_sector(data.get("sector", ""))
            if stock_sector not in sectors_canonical:
                continue

        # --- industry (substring match, with sector fallback) ---
        if "industry" in rules:
            rule_industry = rules["industry"].lower()
            stock_industry = (data.get("industry", "") or "").lower()
            stock_sector = (data.get("sector", "") or "").lower()
            # Direct substring match on industry
            industry_match = rule_industry in stock_industry
            # Fallback: "manufacturing" matches "industrials" sector
            if not industry_match:
                industry_sector_map = {
                    "manufacturing": "industrials",
                    "tech": "technology",
                    "finance": "financial services",
                    "pharma": "healthcare",
                    "oil": "energy",
                }
                mapped_sector = industry_sector_map.get(rule_industry, "")
                industry_match = mapped_sector and mapped_sector in stock_sector
            if not industry_match:
                continue

        # --- P/E ratio ---
        if "max_pe_ratio" in rules:
            pe = data.get("pe_ratio", 0) or 0
            if pe <= 0 or pe > rules["max_pe_ratio"]:
                continue
        if "min_pe_ratio" in rules:
            pe = data.get("pe_ratio", 0) or 0
            if pe <= 0 or pe < rules["min_pe_ratio"]:
                continue

        # --- dividend yield ---
        if "min_dividend_yield" in rules:
            div_yield = data.get("dividend_yield", 0) or 0
            if div_yield < rules["min_dividend_yield"]:
                continue

        # --- beta ---
        if "max_beta" in rules:
            beta = data.get("beta")
            if beta is None or beta > rules["max_beta"]:
                continue
        if "min_beta" in rules:
            beta = data.get("beta")
            if beta is None or beta < rules["min_beta"]:
                continue

        # --- revenue growth ---
        if min_revenue_growth is not None and data["revenue_growth"] < min_revenue_growth:
            continue

        # --- positive FCF ---
        if rules.get("positive_fcf"):
            fcf_val = data.get("fcf", 0) or 0
            if fcf_val <= 0:
                continue

        # --- profitable only ---
        if rules.get("profitable_only"):
            net_inc = data.get("net_income", 0) or data.get("profit_margin", 0) or 0
            if net_inc <= 0:
                continue

        # --- ROE ---
        if "min_roe" in rules:
            roe_val = data.get("roe", 0) or 0
            if roe_val < rules["min_roe"]:
                continue

        # --- gross margin ---
        if "min_gross_margin" in rules:
            gm = data.get("gross_margin", 0) or 0
            if gm < rules["min_gross_margin"]:
                continue

        # --- net margin ---
        if "min_net_margin" in rules:
            nm = data.get("profit_margin", 0) or data.get("net_margin", 0) or 0
            if nm < rules["min_net_margin"]:
                continue

        # --- PEG ratio ---
        if "max_peg_ratio" in rules:
            peg = data.get("peg_ratio", 0) or 0
            if peg <= 0 or peg > rules["max_peg_ratio"]:
                continue

        # --- EV/EBITDA ---
        if "max_ev_to_ebitda" in rules:
            eve = data.get("ev_to_ebitda", 0) or 0
            if eve <= 0 or eve > rules["max_ev_to_ebitda"]:
                continue

        # --- current ratio ---
        if "min_current_ratio" in rules:
            cr = data.get("current_ratio", 0) or 0
            if cr < rules["min_current_ratio"]:
                continue

        # --- EPS growth ---
        if "min_eps_growth" in rules:
            epsg = data.get("eps_growth", 0) or 0
            if epsg < rules["min_eps_growth"]:
                continue

        # --- FCF yield ---
        if "min_fcf_yield" in rules:
            fy = data.get("fcf_yield", 0) or 0
            if fy < rules["min_fcf_yield"]:
                continue

        matches.append(ticker)

    matches.sort()
    return matches


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["Swarm Engine"])


def _screen_via_fmp(rules: dict) -> list[str] | None:
    """Use FMP stock screener to find matching stocks."""
    if fmp_client is None or not fmp_client._enabled():
        return None
    params = {}
    if "min_market_cap" in rules:
        params["market_cap_more_than"] = rules["min_market_cap"] * 1e9
    if "max_market_cap" in rules:
        params["market_cap_lower_than"] = rules["max_market_cap"] * 1e9
    if "sectors" in rules and rules["sectors"]:
        sector = rules["sectors"][0]
        fmp_sector_reverse = {
            "technology": "Technology",
            "health care": "Healthcare",
            "financials": "Financial Services",
            "consumer discretionary": "Consumer Cyclical",
            "consumer staples": "Consumer Defensive",
            "communication services": "Communication Services",
            "industrials": "Industrials",
            "materials": "Basic Materials",
            "real estate": "Real Estate",
            "utilities": "Utilities",
            "energy": "Energy",
            "healthcare": "Healthcare",
            "financial services": "Financial Services",
            "manufacturing": "Industrials",
        }
        normalized = _normalize_sector(sector)
        fmp_name = fmp_sector_reverse.get(normalized, sector.title())
        params["sector"] = fmp_name
    if "min_beta" in rules:
        params["beta_more_than"] = rules["min_beta"]
    if "max_beta" in rules:
        params["beta_lower_than"] = rules["max_beta"]
    results = fmp_client.screen_stocks(**params)
    if not results:
        return None
    symbols = [r.get("symbol", "") for r in results if r.get("symbol")]
    logger.info("FMP screener returned %d stocks", len(symbols))
    return symbols


def _fetch_fmp_fundamentals(symbols: list[str]) -> dict[str, dict]:
    """Fetch detailed fundamentals from FMP for a list of symbols."""
    if fmp_client is None or not fmp_client._enabled():
        return {}
    profiles = fmp_client.batch_profile(symbols)
    if not profiles:
        return {}
    ratio_symbols = list(profiles.keys())[:50]
    ratios = fmp_client.batch_ratios(ratio_symbols)
    universe = {}
    for sym, profile in profiles.items():
        ratio = ratios.get(sym)
        fundamentals = fmp_client.fmp_to_fundamentals(profile, ratio)
        # Enrich with Dexter institutional-grade ratios
        if dexter_client:
            try:
                fundamentals = dexter_client.enrich_fundamentals(fundamentals, sym)
            except Exception as e:
                logger.debug("Dexter enrichment failed for %s: %s", sym, e)
        universe[sym] = fundamentals
    logger.info("FMP fundamentals loaded for %d stocks", len(universe))
    return universe


# ---------------------------------------------------------------------------
# Full universe cache — progressive fundamentals fetching for 9,920+ tickers
# ---------------------------------------------------------------------------
import os
import time
import threading
from pathlib import Path

_UNIVERSE_CACHE_DIR = Path(__file__).parent / "data" / "universe_cache"
_UNIVERSE_CACHE_FILE = _UNIVERSE_CACHE_DIR / "fundamentals.json"
_UNIVERSE_CACHE_MAX_AGE = 7 * 86400  # 7 days
_universe_fetch_lock = threading.Lock()
_universe_fetch_running = False


def _load_full_universe_cache() -> dict[str, dict] | None:
    """Load cached fundamentals for full universe. Returns None if cache is empty/stale."""
    if not _UNIVERSE_CACHE_FILE.exists():
        # Trigger background fetch on first call
        _trigger_universe_fetch()
        return None
    try:
        raw = json.loads(_UNIVERSE_CACHE_FILE.read_text())
        if time.time() - raw.get("fetched_at", 0) > _UNIVERSE_CACHE_MAX_AGE:
            _trigger_universe_fetch()  # refresh in background
            # Still use stale data for this request
        stocks = raw.get("stocks", {})
        if len(stocks) < 100:
            _trigger_universe_fetch()
            return None
        return stocks
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _trigger_universe_fetch():
    """Kick off background thread to fetch fundamentals for full universe."""
    global _universe_fetch_running
    with _universe_fetch_lock:
        if _universe_fetch_running:
            return
        _universe_fetch_running = True
    t = threading.Thread(target=_fetch_universe_fundamentals_bg, daemon=True)
    t.start()


def _fetch_universe_fundamentals_bg():
    """Background: fetch fundamentals for all US tickers from SEC EDGAR universe.

    Uses strategy_engine's get_sp500_fundamentals() which already handles
    yfinance -> Yahoo v8 API fallbacks with rate limit backoff.
    Fetches in batches, saves progressively.
    """
    global _universe_fetch_running
    try:
        from research.stock_universe import get_universe
        from strategy_engine import get_sp500_fundamentals

        # Get all US ticker symbols
        all_stocks = get_universe(exchange=None, include_hkex=False)
        all_tickers = [s["ticker"] if isinstance(s, dict) else s for s in all_stocks]
        logger.info("Universe fetch: %d tickers to process", len(all_tickers))

        # Load existing cache to do incremental updates
        existing = {}
        if _UNIVERSE_CACHE_FILE.exists():
            try:
                raw = json.loads(_UNIVERSE_CACHE_FILE.read_text())
                existing = raw.get("stocks", {})
            except Exception:
                pass

        # Only fetch tickers we don't already have (or that are stale)
        need_fetch = [t for t in all_tickers if t not in existing]
        if not need_fetch:
            logger.info("Universe cache is complete (%d tickers), skipping fetch", len(existing))
            return

        # Fetch in chunks of 500 (get_sp500_fundamentals does its own batching of 20 internally)
        CHUNK = 500
        for i in range(0, len(need_fetch), CHUNK):
            chunk = need_fetch[i:i + CHUNK]
            logger.info("Universe fetch: chunk %d-%d of %d", i, i + len(chunk), len(need_fetch))
            try:
                chunk_data = get_sp500_fundamentals(symbols=chunk)
                existing.update(chunk_data)
                # Save after each chunk (progressive)
                _save_universe_cache(existing)
                logger.info("Universe cache: %d total stocks cached", len(existing))
            except Exception as e:
                logger.warning("Universe fetch chunk failed: %s", e)
                # Save what we have and stop
                if existing:
                    _save_universe_cache(existing)
                break
    except Exception as e:
        logger.error("Universe fundamentals fetch failed: %s", e)
    finally:
        with _universe_fetch_lock:
            _universe_fetch_running = False


def _save_universe_cache(stocks: dict):
    """Save universe fundamentals cache to disk."""
    _UNIVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import tempfile
    cache = {"fetched_at": time.time(), "count": len(stocks), "stocks": stocks}
    tmp = str(_UNIVERSE_CACHE_FILE) + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, str(_UNIVERSE_CACHE_FILE))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)


@router.get(
    "/api/generate-asset/universe-status",
    summary="Check universe fundamentals cache status",
    tags=["Swarm Engine"],
)
async def get_universe_status():
    """Returns status of the full universe fundamentals cache."""
    if not _UNIVERSE_CACHE_FILE.exists():
        return {"cached": 0, "status": "not_started", "stale": True}
    try:
        raw = json.loads(_UNIVERSE_CACHE_FILE.read_text())
        count = raw.get("count", 0)
        fetched_at = raw.get("fetched_at", 0)
        age_hours = (time.time() - fetched_at) / 3600
        return {
            "cached": count,
            "status": "fetching" if _universe_fetch_running else "ready",
            "age_hours": round(age_hours, 1),
            "stale": age_hours > 168,  # 7 days
        }
    except Exception:
        return {"cached": 0, "status": "error", "stale": True}


@router.post(
    "/api/generate-asset/universe-refresh",
    summary="Trigger background refresh of universe fundamentals",
    tags=["Swarm Engine"],
)
async def trigger_universe_refresh():
    """Trigger a background fetch of fundamentals for all US tickers."""
    if _universe_fetch_running:
        return {"status": "already_running"}
    _trigger_universe_fetch()
    return {"status": "started"}


@router.post(
    "/api/generate-asset/screen",
    response_model=ScreenResponse,
    summary="Screen stocks from a natural-language prompt",
)
async def screen_stocks(body: ScreenRequest) -> ScreenResponse:
    """Turn a natural-language prompt into a screened stock basket.

    Uses a 3-path fallback:
      1. FMP stock screener (covers ALL US stocks)
      2. S&P 500 fundamentals cache (via strategy_engine)
      3. 83-stock mock universe
    """
    # Use structured rules if provided, otherwise parse from prompt
    if body.rules:
        rules = dict(body.rules)
        # If prompt also provided, parse it and merge (structured rules take priority)
        if body.prompt and body.prompt.strip():
            parsed = await intent_parser(body.prompt)
            for k, v in parsed.items():
                if k not in rules:
                    rules[k] = v
    elif body.prompt and body.prompt.strip():
        rules = await intent_parser(body.prompt)
    else:
        rules = {}

    # If rules is empty, show full mock universe with a note
    if not rules:
        all_tickers = list(MOCK_UNIVERSE.keys())
        return ScreenResponse(
            tickers=all_tickers,
            rules={},
            count=len(all_tickers),
            universe_size=len(MOCK_UNIVERSE),
            notes="No specific filters applied — showing full universe.",
        )

    tickers = []
    universe_size = 0

    # Path 1: FMP screener (covers ALL US stocks)
    fmp_symbols = _screen_via_fmp(rules)
    if fmp_symbols:
        fmp_universe = _fetch_fmp_fundamentals(fmp_symbols)
        if fmp_universe:
            sp500_rules = dict(rules)
            if "min_market_cap" in sp500_rules:
                sp500_rules["min_market_cap"] = sp500_rules["min_market_cap"] * 1e9
            if "max_market_cap" in sp500_rules:
                sp500_rules["max_market_cap"] = sp500_rules["max_market_cap"] * 1e9
            if "min_fcf" in sp500_rules:
                sp500_rules["min_fcf"] = sp500_rules["min_fcf"] * 1e6
            tickers = swarm_evaluator(sp500_rules, fmp_universe)
            universe_size = len(fmp_universe)

    # Path 2: SP500 cache fallback
    if not tickers:
        try:
            from strategy_engine import get_sp500_fundamentals, SP500_CACHE_FILE
            if SP500_CACHE_FILE.exists():
                universe = get_sp500_fundamentals()
                sp500_rules = dict(rules)
                if "min_market_cap" in sp500_rules:
                    sp500_rules["min_market_cap"] = sp500_rules["min_market_cap"] * 1e9
                if "max_market_cap" in sp500_rules:
                    sp500_rules["max_market_cap"] = sp500_rules["max_market_cap"] * 1e9
                if "min_fcf" in sp500_rules:
                    sp500_rules["min_fcf"] = sp500_rules["min_fcf"] * 1e6
                tickers = swarm_evaluator(sp500_rules, universe)
                universe_size = len(universe)
        except Exception as e:
            logger.debug("SP500 cache fallback failed: %s", e)

    # Path 3: Full universe fundamentals cache (9,920+ tickers from SEC EDGAR)
    if not tickers:
        try:
            full_universe = _load_full_universe_cache()
            if full_universe:
                full_rules = dict(rules)
                if "min_market_cap" in full_rules:
                    full_rules["min_market_cap"] = full_rules["min_market_cap"] * 1e9
                if "max_market_cap" in full_rules:
                    full_rules["max_market_cap"] = full_rules["max_market_cap"] * 1e9
                if "min_fcf" in full_rules:
                    full_rules["min_fcf"] = full_rules["min_fcf"] * 1e6
                tickers = swarm_evaluator(full_rules, full_universe)
                universe_size = len(full_universe)
                logger.info("Full universe screener: %d matches from %d stocks", len(tickers), universe_size)
        except Exception as e:
            logger.debug("Full universe cache fallback failed: %s", e)

    # Path 4: Mock universe fallback
    if not tickers:
        tickers = swarm_evaluator(rules, MOCK_UNIVERSE)
        universe_size = len(MOCK_UNIVERSE)

    # Extract unresolved criteria (things we couldn't filter on)
    unresolved = rules.pop("unresolved", [])
    if not isinstance(unresolved, list):
        unresolved = [str(unresolved)]

    # Research pass: if there are unresolved qualitative criteria AND tickers
    # to filter, run scrapling web scraping + LLM analysis
    research_results: dict[str, str] = {}
    if unresolved and tickers:
        try:
            from strategy_engine import research_filter
            # Build sp500_list-like structure with company names for Wikipedia
            # Pull names from stock_universe.py for tickers not in _COMPANY_NAMES
            _universe_names = {}
            try:
                from research.stock_universe import get_universe
                for s in get_universe(include_hkex=False):
                    if isinstance(s, dict) and s.get("ticker") and s.get("company_name"):
                        _universe_names[s["ticker"]] = s["company_name"]
            except Exception:
                pass

            ticker_list = []
            for t in tickers:
                name = _COMPANY_NAMES.get(t) or _universe_names.get(t, t)
                ticker_list.append({"symbol": t, "name": name})

            # Try FMP for names we don't have in the map
            missing = [e for e in ticker_list if e["name"] == e["symbol"]]
            if missing and fmp_client:
                try:
                    import asyncio as _aio
                    profiles = await _aio.get_event_loop().run_in_executor(
                        None, fmp_client.batch_profile, [e["symbol"] for e in missing[:20]]
                    )
                    for entry in missing:
                        p = profiles.get(entry["symbol"])
                        if p and p.get("companyName"):
                            entry["name"] = p["companyName"]
                except Exception:
                    pass

            logger.info("Research pass: %d tickers, unresolved=%s", len(tickers), unresolved)
            filtered, research_results = await research_filter(
                tickers, ticker_list, unresolved
            )
            # Research ran — use results regardless (even if 0 matches)
            tickers = filtered
            unresolved = []  # resolved via research
            logger.info("Research filter: %d -> %d tickers", len(ticker_list), len(filtered))
        except Exception as e:
            logger.warning("Research filter failed, skipping: %s", e)

    # Fallback: use company_researcher for remaining unresolved criteria
    if unresolved and tickers:
        try:
            import asyncio as _aio
            from research.company_researcher import research_company
            loop = _aio.get_running_loop()
            resolved_tickers = []
            for sym in tickers:
                info = await loop.run_in_executor(None, research_company, sym)
                if info:
                    blob = " ".join(str(v) for v in info.values() if v).lower()
                    matched_criteria = 0
                    for criterion in unresolved:
                        keywords = criterion.lower().split()
                        if sum(1 for kw in keywords if kw in blob) >= len(keywords) // 2 + 1:
                            matched_criteria += 1
                    if matched_criteria > 0:
                        resolved_tickers.append(sym)
            if resolved_tickers:
                tickers = resolved_tickers
                unresolved = []
                logger.info("Company research fallback: %d tickers matched", len(resolved_tickers))
        except Exception as e:
            logger.warning("Company research fallback failed: %s", e)

    # Build notes from research results
    notes = ""
    if research_results:
        notes = "; ".join(f"{sym}: {reason}" for sym, reason in research_results.items() if reason)

    return ScreenResponse(
        tickers=tickers,
        rules=rules,
        count=len(tickers),
        universe_size=universe_size,
        unresolved=unresolved,
    )
