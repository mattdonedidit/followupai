"""
RealFollow AI — Full SaaS Platform for Real Estate Agents
Features: Login, Stripe payments, lead scoring, email sender,
          custom branding, voice scripts, market reports, property search
"""

import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import pandas as pd
import sqlite3
import hashlib
import json
import time
import smtplib
import os
import re
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import BytesIO

st.set_page_config(page_title="RealFollow AI", page_icon="🏡",
                   layout="wide", initial_sidebar_state="collapsed")

# ── CONFIG — replace with your actual keys ─────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "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")
STRIPE_KEY        = os.getenv("STRIPE_KEY", "")          # sk_live_...
SMTP_EMAIL        = os.getenv("SMTP_EMAIL", "")           # your Gmail
SMTP_PASS         = os.getenv("SMTP_PASS",  "")           # Gmail app password
RAPIDAPI_KEY      = os.getenv("RAPIDAPI_KEY", "")

# Subscription plans
PLANS = {
    "starter":      {"name": "Starter",      "price": 97,  "leads_per_month": 50,  "features": ["SMS messages","Day 1 only","Email support"]},
    "professional": {"name": "Professional", "price": 197, "leads_per_month": 999, "features": ["Unlimited leads","3-day sequence","A/B variants","Email sender","Lead scoring","Voice scripts"]},
    "team":         {"name": "Team",         "price": 397, "leads_per_month": 999, "features": ["Everything in Pro","5 agents","Market reports","Custom branding","Priority support"]},
}

# ── DATABASE ───────────────────────────────────────────────────────────────────
DB = "realfollow.db"

def init_db():
    c = sqlite3.connect(DB)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE, password_hash TEXT,
            name TEXT, phone TEXT, company TEXT,
            plan TEXT DEFAULT 'starter',
            logo_url TEXT, brand_color TEXT DEFAULT '#6c8aff',
            stripe_customer_id TEXT,
            created_at TEXT, last_login TEXT,
            leads_used_this_month INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, created_at TEXT,
            total_leads INTEGER, message_type TEXT,
            tone TEXT, sequence_on INTEGER, ab_on INTEGER
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER, user_id INTEGER,
            lead_name TEXT, lead_email TEXT,
            seq_day INTEGER, variant TEXT,
            message TEXT, lead_score INTEGER,
            score_reason TEXT,
            sent INTEGER DEFAULT 0, responded INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, amount REAL,
            plan TEXT, stripe_id TEXT,
            created_at TEXT, status TEXT
        );
        CREATE TABLE IF NOT EXISTS drip_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, name TEXT,
            lead_name TEXT, lead_email TEXT, lead_phone TEXT,
            messages TEXT, schedule TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT, next_send_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sms_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, from_number TEXT,
            lead_name TEXT, message TEXT,
            direction TEXT, created_at TEXT, read_integer INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS roi_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, lead_name TEXT,
            event_type TEXT, commission_est REAL,
            notes TEXT, created_at TEXT
        );
    """)
    # Migrate old databases — add missing columns safely
    migrations = [
        ("messages", "user_id",      "INTEGER DEFAULT 0"),
        ("messages", "lead_email",   "TEXT DEFAULT ''"),
        ("messages", "lead_score",   "INTEGER DEFAULT 0"),
        ("messages", "score_reason", "TEXT DEFAULT ''"),
        ("messages", "sent",         "INTEGER DEFAULT 0"),
        ("messages", "seq_day",      "INTEGER DEFAULT 1"),
        ("campaigns","user_id",      "INTEGER DEFAULT 0"),
        ("users",    "brand_color",  "TEXT DEFAULT '#6c8aff'"),
        ("users",    "logo_url",     "TEXT DEFAULT ''"),
        ("users",    "active",       "INTEGER DEFAULT 1"),
        ("campaigns","sequence_on",   "INTEGER DEFAULT 0"),
        ("campaigns","ab_on",         "INTEGER DEFAULT 0"),
        ("campaigns","tone",          "TEXT DEFAULT ''"),
        ("campaigns","message_type",  "TEXT DEFAULT ''"),
        ("campaigns","total_leads",   "INTEGER DEFAULT 0"),
    ]
    for table, col, col_type in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except Exception:
            pass  # column already exists
    c.commit(); c.close()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Drip campaign helpers ──────────────────────────────────────────────────────
def save_drip(uid, name, lead_name, lead_email, lead_phone, messages, schedule):
    c = sqlite3.connect(DB)
    next_send = (datetime.now() + timedelta(days=1)).isoformat()
    c.execute("INSERT INTO drip_campaigns VALUES(NULL,?,?,?,?,?,?,?,?,?)",
              (uid, name, lead_name, lead_email, lead_phone,
               json.dumps(messages), json.dumps(schedule),
               "active", datetime.now().isoformat(), next_send))
    c.commit(); c.close()

def get_drips(uid):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM drip_campaigns WHERE user_id=? ORDER BY created_at DESC",
                     (uid,)).fetchall()
    c.close(); return [dict(r) for r in rows]

def get_roi_events(uid):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM roi_events WHERE user_id=? ORDER BY created_at DESC",
                     (uid,)).fetchall()
    c.close(); return [dict(r) for r in rows]

def save_roi_event(uid, lead_name, event_type, commission):
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO roi_events VALUES(NULL,?,?,?,?,?,?)",
              (uid, lead_name, event_type, commission, "",
               datetime.now().isoformat()))
    c.commit(); c.close()

def get_sms_inbox(uid):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM sms_inbox WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
                     (uid,)).fetchall()
    c.close(); return [dict(r) for r in rows]

def generate_weekly_report(client, uid, agent_info):
    campaigns, messages = get_analytics()
    roi_events = get_roi_events(uid)
    total_commission = sum(r.get("commission_est",0) for r in roi_events)
    responded = sum(1 for m in messages if m.get("responded"))
    prompt = f"""Write a professional weekly performance report for a real estate agent.

Agent: {agent_info.get('name','')} | {agent_info.get('company','')}
Week ending: {datetime.now().strftime('%B %d, %Y')}

Stats:
- Total campaigns run: {len(campaigns)}
- Messages sent: {len(messages)}
- Responses received: {responded}
- Response rate: {f"{responded/len(messages):.0%}" if messages else "N/A"}
- Estimated commission pipeline: ${total_commission:,.0f}
- ROI events logged: {len(roi_events)}

Write an encouraging, professional weekly summary with:
1. Performance highlights
2. What's working
3. 3 action items for next week
4. Motivational closing

Sign from: {agent_info.get('name','')}"""
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514",
            max_tokens=600, messages=[{"role":"user","content":prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Error generating report: {e}"

def create_user(email, pw, name, phone, company):
    try:
        c = sqlite3.connect(DB)
        c.execute("INSERT INTO users (email,password_hash,name,phone,company,created_at) VALUES (?,?,?,?,?,?)",
                  (email.lower(), hash_pw(pw), name, phone, company, datetime.now().isoformat()))
        c.commit(); c.close(); return True
    except sqlite3.IntegrityError:
        return False

def get_user(email, pw):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    u = c.execute("SELECT * FROM users WHERE email=? AND password_hash=? AND active=1",
                  (email.lower(), hash_pw(pw))).fetchone()
    c.close()
    return dict(u) if u else None

def update_user(uid, **kwargs):
    c = sqlite3.connect(DB)
    for k, v in kwargs.items():
        c.execute(f"UPDATE users SET {k}=? WHERE id=?", (v, uid))
    c.commit(); c.close()

def get_user_by_id(uid):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close(); return dict(u) if u else None

def save_campaign(uid, leads, mtype, tone, seq, ab):
    c = sqlite3.connect(DB)
    cur = c.execute(
        "INSERT INTO campaigns (user_id,created_at,total_leads,message_type,tone,sequence_on,ab_on) VALUES (?,?,?,?,?,?,?)",
        (uid, datetime.now().isoformat(), leads, mtype, tone, int(seq), int(ab)))
    cid = cur.lastrowid; c.commit(); c.close(); return cid

def save_messages(cid, uid, lead_name, lead_email, msgs, scores):
    c = sqlite3.connect(DB)
    for day, variants in msgs.items():
        for var, txt in variants.items():
            score_data = scores.get(lead_name, {})
            c.execute("INSERT INTO messages VALUES(NULL,?,?,?,?,?,?,?,?,?,0,0,?)",
                (cid, uid, lead_name, lead_email, day, var, txt,
                 score_data.get("score", 0), score_data.get("reason",""),
                 datetime.now().isoformat()))
    c.commit(); c.close()

def get_user_messages(uid, limit=200):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM messages WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                     (uid, limit)).fetchall()
    c.close(); return [dict(r) for r in rows]

def get_user_campaigns(uid):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM campaigns WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
                     (uid,)).fetchall()
    c.close(); return [dict(r) for r in rows]

def mark_sent(mid):
    c = sqlite3.connect(DB)
    c.execute("UPDATE messages SET sent=1 WHERE id=?", (mid,))
    c.commit(); c.close()

def mark_responded(mid):
    c = sqlite3.connect(DB)
    c.execute("UPDATE messages SET responded=1 WHERE id=?", (mid,))
    c.commit(); c.close()

init_db()

# ── CSS ────────────────────────────────────────────────────────────────────────
def get_css(brand_color="#6c8aff"):
    return f"""<style>
.stApp {{ background:#0f1117; }}
.hero {{ background:linear-gradient(135deg,#1a1f36,#2d3561); padding:40px;
         border-radius:16px; margin-bottom:32px; border:1px solid #3d4575; }}
.hero h1 {{ font-size:40px; font-weight:800; color:#fff; margin:0; }}
.hero p {{ font-size:17px; color:#a0aec0; margin-top:8px; }}
.hl {{ color:{brand_color}; }}
.kpi {{ background:#1a1f36; border:1px solid #2d3561; border-radius:12px;
        padding:20px; text-align:center; }}
.kpi-n {{ font-size:28px; font-weight:800; color:{brand_color}; }}
.kpi-l {{ font-size:12px; color:#718096; margin-top:4px; }}
.mcard {{ background:#1a1f2e; border:1px solid #2d3561; border-radius:12px;
          padding:20px; margin-bottom:12px; }}
.lname {{ font-size:15px; font-weight:700; color:{brand_color}; margin-bottom:8px; }}
.mtxt  {{ color:#e2e8f0; line-height:1.6; white-space:pre-wrap; font-size:14px; }}
.dbadge {{ background:#2d3561; color:{brand_color}; border-radius:20px; padding:3px 10px;
           font-size:11px; font-weight:700; display:inline-block; margin-bottom:6px; margin-right:4px; }}
.vbadge {{ background:#1a3a2a; color:#68d391; border-radius:20px; padding:3px 10px;
           font-size:11px; font-weight:700; display:inline-block; margin-bottom:6px; margin-right:4px; }}
.sbadge {{ border-radius:20px; padding:3px 10px; font-size:11px;
           font-weight:700; display:inline-block; margin-bottom:6px; }}
.score-high {{ background:#1a3a2a; color:#68d391; }}
.score-mid  {{ background:#3a3020; color:#ffd166; }}
.score-low  {{ background:#3a1a1a; color:#ff4d6d; }}
.tip {{ background:#1a2535; border-left:4px solid {brand_color}; padding:14px;
        border-radius:0 8px 8px 0; color:#a0aec0; font-size:13px; margin-bottom:14px; }}
.ok  {{ background:linear-gradient(135deg,#1a3a2a,#1f4a35); border:1px solid #2d6a4a;
        border-radius:12px; padding:16px; margin-bottom:20px; color:#68d391; font-weight:600; }}
.plan-card {{ background:#1a1f2e; border-radius:16px; padding:28px; text-align:center; }}
.auth-box {{ max-width:420px; margin:60px auto; background:#1a1f2e;
             border:1px solid #2d3561; border-radius:16px; padding:40px; }}
.auth-title {{ font-size:28px; font-weight:800; color:#fff; text-align:center; margin-bottom:8px; }}
.auth-sub {{ color:#a0aec0; text-align:center; margin-bottom:28px; font-size:15px; }}
</style>"""

st.markdown(get_css(), unsafe_allow_html=True)

# ── SESSION STATE ──────────────────────────────────────────────────────────────
if "user" not in st.session_state:    st.session_state.user = None
if "auth_tab" not in st.session_state: st.session_state.auth_tab = "login"

# ══════════════════════════════════════════════════════════════════════════════
# AUTH SCREENS
# ══════════════════════════════════════════════════════════════════════════════
if not st.session_state.user:
    st.markdown(get_css(), unsafe_allow_html=True)
    st.markdown('<div style="text-align:center;padding:40px 0 20px">'
                '<span style="font-size:48px">🏡</span>'
                '<h1 style="color:#fff;margin:8px 0">RealFollow <span style="color:#6c8aff">AI</span></h1>'
                '<p style="color:#a0aec0">The AI follow-up platform for real estate agents</p></div>',
                unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1,2,1])
    with col2:
        login_tab, signup_tab = st.tabs(["🔑 Sign In", "✨ Create Account"])

        with login_tab:
            email_l = st.text_input("Email", placeholder="you@email.com", key="l_email")
            pass_l  = st.text_input("Password", type="password", key="l_pass")
            if st.button("Sign In →", key="login_btn", use_container_width=True):
                user = get_user(email_l, pass_l)
                if user:
                    st.session_state.user = user
                    update_user(user["id"], last_login=datetime.now().isoformat())
                    st.rerun()
                else:
                    st.error("Invalid email or password.")

        with signup_tab:
            s_name    = st.text_input("Full Name *",   placeholder="Sarah Johnson",   key="s_name")
            s_email   = st.text_input("Email *",       placeholder="sarah@email.com", key="s_email")
            s_pass    = st.text_input("Password *",    type="password",               key="s_pass")
            s_phone   = st.text_input("Phone",         placeholder="(305) 555-0100",  key="s_phone")
            s_company = st.text_input("Company",       placeholder="Keller Williams", key="s_company")

            st.markdown('<div style="background:#1a2535;border-radius:8px;padding:12px;'
                        'color:#a0aec0;font-size:13px;margin:8px 0">Start with a '
                        '<b style="color:#6c8aff">14-day free trial</b> — no credit card needed.</div>',
                        unsafe_allow_html=True)

            if st.button("Create Free Account →", key="signup_btn", use_container_width=True):
                if not all([s_name, s_email, s_pass]):
                    st.error("Name, email, and password are required.")
                elif len(s_pass) < 6:
                    st.error("Password must be at least 6 characters.")
                elif create_user(s_email, s_pass, s_name, s_phone, s_company):
                    user = get_user(s_email, s_pass)
                    st.session_state.user = user
                    st.success("Account created! Welcome to RealFollow AI.")
                    st.rerun()
                else:
                    st.error("An account with this email already exists.")

    st.markdown('<br><div style="text-align:center;color:#4a5568;font-size:13px">'
                'RealFollow AI · Powered by Claude AI · Built for Real Estate Professionals'
                '</div>', unsafe_allow_html=True)
    st.stop()

# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP (logged in)
# ══════════════════════════════════════════════════════════════════════════════
user = st.session_state.user
brand_color = user.get("brand_color") or "#6c8aff"
st.markdown(get_css(brand_color), unsafe_allow_html=True)

# ── Top bar ────────────────────────────────────────────────────────────────────
col1, col2, col3 = st.columns([3,1,1])
with col1:
    st.markdown(f'<span style="font-size:22px;font-weight:800;color:#fff">🏡 RealFollow '
                f'<span style="color:{brand_color}">AI</span></span> '
                f'<span style="color:#718096;font-size:14px;margin-left:12px">'
                f'Welcome, {user.get("name","")}</span>', unsafe_allow_html=True)
with col2:
    plan = user.get("plan","starter")
    st.markdown(f'<div style="text-align:center;background:#1a1f2e;border:1px solid #2d3561;'
                f'border-radius:8px;padding:6px 12px;color:{brand_color};'
                f'font-size:12px;font-weight:700">{plan.upper()} PLAN</div>',
                unsafe_allow_html=True)
with col3:
    if st.button("Sign Out", key="signout"):
        st.session_state.user = None
        st.rerun()

st.markdown("<hr style='border-color:#2d3561;margin:12px 0'>", unsafe_allow_html=True)

# ── Sidebar (settings) ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(f"### ⚙️ Settings")
    tone         = st.selectbox("Tone", ["Friendly & Personal","Professional & Formal",
                                          "Casual & Conversational","Urgent & Direct"])
    message_type = st.selectbox("Type", ["Text Message (SMS)","Email",
                                          "DM (Instagram/Facebook)","Voicemail Script"])
    seq_mode = st.toggle("📅 3-Message Sequence", value=True)
    ab_mode  = st.toggle("🔀 A/B Variants",       value=True)
    scoring  = st.toggle("🎯 Lead Scoring",        value=True)

    st.markdown("---")
    st.markdown("### 👤 Your Profile")
    agent_name    = st.text_input("Name",    value=user.get("name",""),    key="p_name")
    agent_phone   = st.text_input("Phone",   value=user.get("phone",""),   key="p_phone")
    agent_company = st.text_input("Company", value=user.get("company",""), key="p_company")
    brand_color_input = st.color_picker("Brand Color", value=brand_color)

    if st.button("Save Profile"):
        update_user(user["id"], name=agent_name, phone=agent_phone,
                    company=agent_company, brand_color=brand_color_input)
        st.session_state.user = get_user_by_id(user["id"])
        st.success("Saved!")
        st.rerun()

    st.markdown("---")
    sample = pd.DataFrame({
        "Name":["John Smith","Maria Garcia"],
        "Email":["john@email.com","maria@email.com"],
        "Phone":["305-555-0101","786-555-0202"],
        "Property":["3/2 Coral Gables","Brickell Condo"],
        "Last Contact":["3 months ago","6 months ago"],
        "Budget":["$600k","$400k"],
        "Notes":["Wants pool","Pre-approved"],
    })
    st.download_button("📥 Sample CSV", sample.to_csv(index=False),
                        "sample_leads.csv","text/csv")

# ── AI helpers ─────────────────────────────────────────────────────────────────
def ai_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def score_lead(client, lead_info):
    """Returns {"score": 1-10, "reason": "..."}"""
    prompt = f"""Rate this real estate lead from 1-10 on likelihood to respond to a follow-up message.

Lead info:
{chr(10).join(f'- {k}: {v}' for k,v in lead_info.items() if v)}

Consider: recency of last contact, specificity of their needs, budget clarity, engagement signals.

Respond in JSON only: {{"score": 7, "reason": "Active 3 months ago, specific property needs, clear budget"}}"""
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514",
            max_tokens=100, messages=[{"role":"user","content":prompt}])
        text = resp.content[0].text.strip()
        text = re.sub(r'```json|```','',text).strip()
        return json.loads(text)
    except Exception:
        return {"score": 5, "reason": "Could not analyze"}

def _generate_single(client, lead_info, agent_info, day, var, mtype, tone_style, voice):
    """Generate one message — called in parallel."""
    day_ctx = {
        1: "FIRST follow-up. Warm, reference last conversation, reintroduce gently.",
        3: "DAY 3. No response yet. More specific about value you can offer.",
        7: "DAY 7 — final. Brief, genuine, leave the door open.",
    }
    var_ctx = {
        "A": "Straightforward and direct.",
        "B": "Start with a market insight or hook, then transition naturally.",
    }
    mtype_use = "Voicemail Script" if voice else mtype
    prompt = f"""You are a top real estate agent writing a personalized follow-up {mtype_use}.

Agent: {agent_info.get('name','')} | {agent_info.get('phone','')} | {agent_info.get('company','')}

Lead:
{chr(10).join(f'- {k}: {v}' for k,v in lead_info.items() if v)}

Sequence: {day_ctx.get(day, f'Day {day} follow-up')}
Style: {var_ctx[var]}
Tone: {tone_style}

Rules: use real name, reference specific details, natural not salesy, soft CTA.
SMS=under 160 chars. Email=start with Subject:. Voicemail=30-45 second script.
Write ONLY the message."""
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514",
            max_tokens=350, messages=[{"role":"user","content":prompt}])
        return (day, var, resp.content[0].text.strip())
    except Exception as e:
        return (day, var, f"Error: {e}")


def generate_messages(client, lead_info, agent_info, seq, ab, mtype, tone_style, voice=False):
    days     = [1,3,7] if seq else [1]
    variants = ["A","B"] if ab else ["A"]
    results  = {day: {} for day in days}

    # Generate all messages in parallel — 3x faster
    tasks = [(day, var) for day in days for var in variants]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_generate_single, client, lead_info, agent_info,
                           day, var, mtype, tone_style, voice): (day, var)
            for day, var in tasks
        }
        for future in as_completed(futures):
            try:
                day, var, text = future.result()
                results[day][var] = text
            except Exception as e:
                day, var = futures[future]
                results[day][var] = f"Error: {e}"
    return results

def generate_market_report(client, city, agent_info):
    prompt = f"""Generate a professional real estate market report for {city}.
Include: market temperature, price trends, days on market, inventory levels,
buyer/seller market assessment, and 3 key talking points an agent can use with clients.
Format it as a clean, professional text report an agent would email to leads.
Sign it from: {agent_info.get('name','')} | {agent_info.get('company','')} | {agent_info.get('phone','')}"""
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514",
            max_tokens=800, messages=[{"role":"user","content":prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Error generating report: {e}"

def search_properties(city, budget, bedrooms=""):
    if not RAPIDAPI_KEY:
        return []
    try:
        # Try the Zillow Real Estate API search by location
        url = "https://zillow-real-estate-api.p.rapidapi.com/search"
        budget_clean = str(budget).replace("$","").replace("k","000").replace(",","").strip()
        params = {
            "location": city,
            "status":   "forSale",
            "maxPrice": budget_clean,
        }
        if bedrooms:
            params["minBeds"] = str(bedrooms)

        r = requests.get(url,
            headers={"X-RapidAPI-Key":  RAPIDAPI_KEY,
                     "X-RapidAPI-Host": "zillow-real-estate-api.p.rapidapi.com"},
            params=params, timeout=10)

        if r.status_code == 200:
            data  = r.json()
            # Handle different response formats
            props = (data.get("results") or data.get("props") or
                     data.get("listings") or data.get("data") or [])
            if isinstance(props, dict):
                props = list(props.values())
            results = []
            for p in props[:5]:
                results.append({
                    "address": (p.get("address") or p.get("streetAddress") or
                                p.get("location") or ""),
                    "price":   (p.get("price") or p.get("listPrice") or
                                p.get("unformattedPrice") or ""),
                    "beds":    (p.get("bedrooms") or p.get("beds") or ""),
                    "baths":   (p.get("bathrooms") or p.get("baths") or ""),
                    "sqft":    (p.get("livingArea") or p.get("sqft") or ""),
                    "img":     (p.get("imgSrc") or p.get("image") or
                                p.get("thumbnail") or ""),
                })
            return results
    except Exception as e:
        st.warning(f"Property search error: {e}")
    return []

def send_email(to_email, subject, body, agent_name, agent_email=None):
    if not SMTP_EMAIL or not SMTP_PASS:
        return False, "Email not configured"
    try:
        msg = MIMEMultipart()
        msg["From"]    = f"{agent_name} <{SMTP_EMAIL}>"
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASS)
        server.send_message(msg)
        server.quit()
        return True, "Sent"
    except Exception as e:
        return False, str(e)

# ── HERO ───────────────────────────────────────────────────────────────────────
st.markdown(f"""<div class="hero">
<h1>🏡 RealFollow <span class="hl">AI</span></h1>
<p>Personalized follow-up sequences · Lead scoring · Market reports · Direct email sending</p>
</div>""", unsafe_allow_html=True)

msgs_all = get_user_messages(user["id"])
camps    = get_user_campaigns(user["id"])
responded_ct = sum(1 for m in msgs_all if m.get("responded"))
rr = f"{responded_ct/len(msgs_all):.0%}" if msgs_all else "—"

c1,c2,c3,c4 = st.columns(4)
for col,n,l in [(c1,len(camps),"Campaigns"),(c2,len(msgs_all),"Messages"),
                 (c3,responded_ct,"Responses"),(c4,rr,"Response Rate")]:
    col.markdown(f'<div class="kpi"><div class="kpi-n">{n}</div>'
                 f'<div class="kpi-l">{l}</div></div>', unsafe_allow_html=True)
st.markdown("<br>", unsafe_allow_html=True)

# ── TABS ───────────────────────────────────────────────────────────────────────
tabs = st.tabs(["📤 Bulk Upload","✍️ Single Lead","🛠️ AI Tools",
                 "📊 Analytics","💰 ROI Tracker","📅 Drip Campaigns",
                 "📱 SMS Inbox","🔗 CRM Export","📚 Script Library",
                 "📄 Market Report","🎨 Branding","💳 Billing","❓ How It Works"])
t1,t2,t3,t4,t_roi,t_drip,t_sms,t5,t_scripts,t6,t7,t8,t9 = tabs

# ══ TAB 1: BULK ═══════════════════════════════════════════════════════════════
with t1:
    st.markdown("### Upload Your Lead List")
    st.markdown('<div class="tip">💡 CSV needs at minimum <b>Name</b> and <b>Email</b> columns. '
                'Download sample from sidebar.</div>', unsafe_allow_html=True)

    uploaded = st.file_uploader("Upload CSV or Excel", type=["csv","xlsx","xls"])
    if uploaded:
        try:
            df = pd.read_csv(uploaded) if uploaded.name.endswith(".csv") else pd.read_excel(uploaded)
            st.success(f"✅ {len(df)} leads loaded")
            st.dataframe(df, use_container_width=True)
            st.markdown("---")
            col1,col2 = st.columns(2)
            with col1:
                max_leads = st.slider("Leads to process", 1, min(len(df),30), min(len(df),5))
            with col2:
                location = st.text_input("City/Area", placeholder="Miami, FL")
            voice_scripts = st.checkbox("Also generate voicemail scripts")
            extra = st.text_area("Why reaching out now? (optional)",
                                  placeholder="Just listed a property that matches exactly...", height=60)

            total_msgs = max_leads * (3 if seq_mode else 1) * (2 if ab_mode else 1)
            st.info(f"Will generate {total_msgs} messages" +
                    (" + lead scores" if scoring else "") +
                    (" + voicemail scripts" if voice_scripts else ""))

            if st.button("🚀 Generate All Messages", key="bulk"):
                client = ai_client()
                agent_info = {"name":agent_name,"phone":agent_phone,"company":agent_company}
                cid = save_campaign(user["id"], max_leads, message_type, tone, seq_mode, ab_mode)
                results = []
                progress = st.progress(0)
                status   = st.empty()

                for idx,(_, row) in enumerate(df.head(max_leads).iterrows()):
                    name  = str(row.get("Name", row.get("name", f"Lead {idx+1}")))
                    email = str(row.get("Email", row.get("email", "")))
                    status.text(f"Processing {name} ({idx+1}/{max_leads})...")
                    progress.progress(idx/max_leads)

                    lead_info = {col: str(row[col]) for col in df.columns
                                 if pd.notna(row[col]) and str(row[col]).strip()}
                    if extra: lead_info["Context"] = extra

                    # Score lead
                    score_data = {}
                    if scoring:
                        score_data[name] = score_lead(client, lead_info)

                    # Generate messages
                    msgs = generate_messages(client, lead_info, agent_info,
                                              seq_mode, ab_mode, message_type, tone)

                    # Voice scripts
                    voice_msgs = {}
                    if voice_scripts:
                        voice_msgs = generate_messages(client, lead_info, agent_info,
                                                        False, False, "Voicemail Script", tone, voice=True)

                    save_messages(cid, user["id"], name, email, msgs, score_data)
                    results.append({"name":name,"email":email,"messages":msgs,
                                    "voice":voice_msgs,"score":score_data.get(name,{})})

                progress.progress(1.0); status.text("Done!")
                st.markdown(f'<div class="ok">✅ Generated messages for {len(results)} leads! '
                             f'Campaign #{cid} saved.</div>', unsafe_allow_html=True)

                # Export buttons
                rows = []
                for r in results:
                    for day, vars_ in r["messages"].items():
                        for var, txt in vars_.items():
                            rows.append({"Lead":r["name"],"Email":r["email"],
                                         "Day":f"Day {day}","Version":var,"Message":txt,
                                         "Score":r["score"].get("score",""),
                                         "Score Reason":r["score"].get("reason","")})
                exp_df = pd.DataFrame(rows)
                c1e,c2e = st.columns(2)
                with c1e:
                    st.download_button("📥 Download All",
                        exp_df.to_csv(index=False), "realfollow_messages.csv","text/csv")
                with c2e:
                    fub = [{"Contact Name":r["name"],"Email":r["email"],
                            "Note":r["messages"].get(1,{}).get("A",""),
                            "Stage":"Follow Up","Lead Score":r["score"].get("score","")}
                           for r in results]
                    st.download_button("📥 Follow Up Boss",
                        pd.DataFrame(fub).to_csv(index=False),"followupboss.csv","text/csv")

                # Display
                for r in results:
                    score_d = r.get("score",{})
                    score_n = score_d.get("score",0)
                    score_cls = "score-high" if score_n>=7 else "score-mid" if score_n>=4 else "score-low"
                    score_badge = (f'<span class="sbadge {score_cls}">⭐ {score_n}/10 — '
                                   f'{score_d.get("reason","")}</span>') if score_n else ""

                    st.markdown(f"#### 👤 {r['name']} {score_badge}", unsafe_allow_html=True)
                    for day in sorted(r["messages"].keys()):
                        for var, txt in r["messages"][day].items():
                            # Email send button
                            send_col1, send_col2 = st.columns([4,1])
                            with send_col1:
                                st.markdown(
                                    f'<div class="mcard">'
                                    f'<span class="dbadge">Day {day}</span>'
                                    f'<span class="vbadge">Version {var}</span>'
                                    f'<div class="mtxt">{txt}</div></div>',
                                    unsafe_allow_html=True)
                            with send_col2:
                                if r.get("email") and r["email"] != "nan" and SMTP_EMAIL:
                                    if st.button(f"📧 Send", key=f"send_{r['name']}_{day}_{var}"):
                                        subj = txt.split("\n")[0].replace("Subject:","").strip() \
                                               if "Subject:" in txt else f"Following up — {r['name']}"
                                        body = "\n".join(txt.split("\n")[1:]).strip() \
                                               if "Subject:" in txt else txt
                                        ok, msg = send_email(r["email"], subj, body, agent_name)
                                        if ok: st.success("Sent!")
                                        else:  st.error(f"Failed: {msg}")

                    if voice_scripts and r.get("voice"):
                        with st.expander(f"🎙️ Voicemail script for {r['name']}"):
                            v = r["voice"].get(1,{}).get("A","")
                            st.text_area("Script", v, height=100,
                                          key=f"voice_{r['name']}")

        except Exception as e:
            st.error(f"Error: {e}")

# ══ TAB 2: SINGLE ═════════════════════════════════════════════════════════════
with t2:
    st.markdown("### Generate Messages for One Lead")
    c1,c2 = st.columns(2)
    with c1:
        s_name  = st.text_input("Lead Name *",       placeholder="John Smith")
        s_email = st.text_input("Email",              placeholder="john@email.com")
        s_prop  = st.text_input("Property Interest",  placeholder="3BR Coral Gables")
        s_loc   = st.text_input("Their City",          placeholder="Coral Gables, FL")
    with c2:
        s_last   = st.text_input("Last Contact",  placeholder="3 months ago")
        s_budget = st.text_input("Budget",         placeholder="$500,000")
        s_notes  = st.text_area("Notes", placeholder="Has 2 kids, wants a pool...", height=105)
    s_ctx   = st.text_area("Why reaching out now?",
                             placeholder="Just got a new listing...", height=60)
    s_voice = st.checkbox("Generate voicemail script too", key="sv")

    if st.button("✨ Generate", key="single"):
        if not s_name: st.error("Enter lead name.")
        else:
            with st.spinner("Writing..."):
                client    = ai_client()
                lead_info = {k:v for k,v in [("Name",s_name),("Email",s_email),
                    ("Property",s_prop),("Location",s_loc),("Last Contact",s_last),
                    ("Budget",s_budget),("Notes",s_notes),("Context",s_ctx)] if v}
                agent_info = {"name":agent_name,"phone":agent_phone,"company":agent_company}

                score_data = score_lead(client, lead_info) if scoring else {}
                msgs = generate_messages(client, lead_info, agent_info,
                                          seq_mode, ab_mode, message_type, tone)
                cid = save_campaign(user["id"],1,message_type,tone,seq_mode,ab_mode)
                save_messages(cid, user["id"], s_name, s_email, msgs,
                               {s_name: score_data} if score_data else {})

            if score_data:
                sc  = score_data.get("score",0)
                cls = "score-high" if sc>=7 else "score-mid" if sc>=4 else "score-low"
                st.markdown(f'<span class="sbadge {cls}">⭐ Lead Score: {sc}/10 — '
                             f'{score_data.get("reason","")}</span>', unsafe_allow_html=True)

            all_txt = []
            for day in sorted(msgs.keys()):
                for var, txt in msgs[day].items():
                    col_m, col_s = st.columns([4,1])
                    with col_m:
                        st.markdown(
                            f'<div class="mcard"><div class="lname">📱 {s_name}</div>'
                            f'<span class="dbadge">Day {day}</span>'
                            f'<span class="vbadge">Version {var}</span>'
                            f'<div class="mtxt">{txt}</div></div>',
                            unsafe_allow_html=True)
                    with col_s:
                        if s_email and SMTP_EMAIL:
                            if st.button("📧 Send", key=f"ss_{day}_{var}"):
                                subj = txt.split("\n")[0].replace("Subject:","").strip() \
                                       if "Subject:" in txt else f"Following up"
                                body = "\n".join(txt.split("\n")[1:]).strip() \
                                       if "Subject:" in txt else txt
                                ok, msg = send_email(s_email, subj, body, agent_name)
                                st.success("Sent!") if ok else st.error(msg)
                    all_txt.append(f"Day {day} / Version {var}:\n{txt}\n")

            if s_voice:
                with st.spinner("Writing voicemail script..."):
                    vm = generate_messages(client, lead_info, agent_info,
                                            False, False, "Voicemail Script", tone, voice=True)
                with st.expander("🎙️ Voicemail Script"):
                    st.text_area("Script", vm.get(1,{}).get("A",""), height=150)

            st.download_button("📥 Download", "\n\n".join(all_txt),
                f"messages_{s_name.replace(' ','_')}.txt","text/plain")

# ══ TAB 3: AI TOOLS ═══════════════════════════════════════════════════════════
with t3:
    st.markdown("### AI Tools")

    tool_tabs = st.tabs(["🏠 Property Suggestions", "🛡️ Objection Handler",
                          "🏘️ Neighborhood Report", "📱 Social Media",
                          "📞 Appointment Setter", "🥶 Cold Outreach"])
    tt1, tt2, tt3, tt4, tt5, tt6 = tool_tabs

    # ── Property Suggestions ──────────────────────────────────────────────────
    with tt1:
        st.markdown("#### AI Property Suggestions")
        st.markdown('<div class="tip">Describe what a lead is looking for and get AI-generated '
                    'property suggestions to include in your messages.</div>', unsafe_allow_html=True)
        c1,c2 = st.columns(2)
        with c1:
            ps_city    = st.text_input("City/Area", placeholder="Coral Gables, FL", key="ps_city")
            ps_budget  = st.text_input("Budget", placeholder="$600,000", key="ps_budget")
            ps_beds    = st.text_input("Bedrooms", placeholder="3", key="ps_beds")
        with c2:
            ps_features = st.text_input("Must-haves", placeholder="pool, garage, good schools", key="ps_feat")
            ps_lead     = st.text_input("Lead name (optional)", placeholder="John Smith", key="ps_lead")
            ps_notes    = st.text_area("Extra notes", placeholder="Has 2 kids, works downtown...",
                                        height=68, key="ps_notes")
        if st.button("🏠 Generate Property Suggestions", key="ps_gen"):
            with st.spinner("Finding matching properties..."):
                client = ai_client()
                prompt = f"""You are a top real estate agent in {ps_city}.
A client{f' named {ps_lead}' if ps_lead else ''} is looking for:
- Location: {ps_city}
- Budget: {ps_budget}
- Bedrooms: {ps_beds}
- Must-haves: {ps_features}
- Notes: {ps_notes}

Generate 3 specific, realistic property suggestions that match these criteria.
For each property include: a realistic address, price, beds/baths, key features,
and a 2-sentence pitch about why it matches their needs.
Format as a clean list an agent could paste into a message."""
                try:
                    resp = client.messages.create(model="claude-sonnet-4-20250514",
                        max_tokens=600, messages=[{"role":"user","content":prompt}])
                    result = resp.content[0].text.strip()
                    st.markdown(f'<div class="mcard"><div class="mtxt">{result}</div></div>',
                                unsafe_allow_html=True)
                    st.download_button("📥 Download", result, "property_suggestions.txt", "text/plain")
                except Exception as e:
                    st.error(f"Error: {e}")

    # ── Objection Handler ─────────────────────────────────────────────────────
    with tt2:
        st.markdown("#### Objection Handler")
        st.markdown('<div class="tip">Get the perfect response to any objection a lead throws at you.</div>',
                    unsafe_allow_html=True)
        common_objections = [
            "I'm not ready yet",
            "I'm already working with another agent",
            "The market is too uncertain right now",
            "Interest rates are too high",
            "I need to sell my house first",
            "I want to wait until prices drop",
            "I'm just browsing, not serious",
            "Custom objection...",
        ]
        obj_choice = st.selectbox("Choose objection", common_objections, key="obj_choice")
        if obj_choice == "Custom objection...":
            obj_choice = st.text_input("Type the objection", key="obj_custom")
        obj_tone   = st.selectbox("Response tone", ["Empathetic & Understanding",
                                                      "Confident & Direct","Educational",
                                                      "Story-based"], key="obj_tone")
        obj_type   = st.selectbox("Response format", ["Text Message","Email","In-person script"],
                                   key="obj_type")
        if st.button("🛡️ Generate Response", key="obj_gen"):
            with st.spinner("Writing response..."):
                client = ai_client()
                prompt = f"""You are a top real estate agent. A lead just said: "{obj_choice}"

Write a {obj_tone} response as a {obj_type} that:
- Acknowledges their concern genuinely
- Reframes the objection with a fresh perspective
- Provides real value or insight
- Ends with a soft next step
- Sounds completely natural, not scripted

Agent: {agent_name} | {agent_phone} | {agent_company}
Write ONLY the response."""
                try:
                    resp = client.messages.create(model="claude-sonnet-4-20250514",
                        max_tokens=400, messages=[{"role":"user","content":prompt}])
                    result = resp.content[0].text.strip()
                    st.markdown(f'<div class="mcard"><div class="lname">Response to: "{obj_choice}"</div>'
                                f'<div class="mtxt">{result}</div></div>', unsafe_allow_html=True)
                    st.download_button("📥 Save Response", result,
                                        "objection_response.txt", "text/plain", key="obj_dl")
                    # Save to script library
                    c2 = sqlite3.connect(DB)
                    c2.execute("CREATE TABLE IF NOT EXISTS scripts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                               "user_id INTEGER, category TEXT, title TEXT, content TEXT, created_at TEXT)")
                    c2.execute("INSERT INTO scripts VALUES(NULL,?,?,?,?,?)",
                               (user["id"], "objection", f"Response to: {obj_choice}", result,
                                datetime.now().isoformat()))
                    c2.commit(); c2.close()
                    st.success("Saved to Script Library!")
                except Exception as e:
                    st.error(f"Error: {e}")

    # ── Neighborhood Report ───────────────────────────────────────────────────
    with tt3:
        st.markdown("#### Neighborhood Report")
        st.markdown('<div class="tip">Generate a detailed neighborhood breakdown to send to leads '
                    'considering a specific area.</div>', unsafe_allow_html=True)
        c1,c2 = st.columns(2)
        with c1:
            nb_area   = st.text_input("Neighborhood/Area", placeholder="Brickell, Miami FL", key="nb_area")
            nb_lead   = st.text_input("Lead name (optional)", placeholder="Maria Garcia", key="nb_lead")
        with c2:
            nb_focus  = st.multiselect("Focus areas", ["Schools","Safety","Restaurants & Nightlife",
                                                        "Commute","Parks & Recreation",
                                                        "Property Values","Future Development"],
                                        default=["Schools","Property Values"], key="nb_focus")
        if st.button("🏘️ Generate Neighborhood Report", key="nb_gen"):
            with st.spinner("Researching neighborhood..."):
                client = ai_client()
                prompt = f"""Write a professional neighborhood report for {nb_area}.
{f'This is for a client named {nb_lead}.' if nb_lead else ''}
Focus on: {', '.join(nb_focus)}

Include specific details, real insights, and honest pros/cons.
Format as a professional report an agent would email to a client.
Sign off from: {agent_name} | {agent_company} | {agent_phone}"""
                try:
                    resp = client.messages.create(model="claude-sonnet-4-20250514",
                        max_tokens=800, messages=[{"role":"user","content":prompt}])
                    result = resp.content[0].text.strip()
                    st.text_area("Report", result, height=350, key="nb_result")
                    st.download_button("📥 Download Report", result,
                                        f"neighborhood_{nb_area.replace(' ','_')}.txt",
                                        "text/plain", key="nb_dl")
                except Exception as e:
                    st.error(f"Error: {e}")

    # ── Social Media ──────────────────────────────────────────────────────────
    with tt4:
        st.markdown("#### Social Media Caption Generator")
        st.markdown('<div class="tip">Generate Instagram and Facebook captions for listings, '
                    'market updates, or agent branding posts.</div>', unsafe_allow_html=True)
        c1,c2 = st.columns(2)
        with c1:
            sm_type    = st.selectbox("Post type", ["New Listing","Just Sold","Open House",
                                                      "Market Update","Agent Branding",
                                                      "Client Testimonial","Tips & Advice"],
                                       key="sm_type")
            sm_platform = st.selectbox("Platform", ["Instagram","Facebook","Both"], key="sm_plat")
        with c2:
            sm_details  = st.text_area("Post details",
                                        placeholder="3/2 home in Coral Gables, pool, $599k, "
                                                    "just listed, open house Sunday 2-5pm...",
                                        height=100, key="sm_details")
        sm_hashtags = st.checkbox("Include hashtags", value=True, key="sm_hash")
        sm_emoji    = st.checkbox("Include emojis", value=True, key="sm_emoji")

        if st.button("📱 Generate Caption", key="sm_gen"):
            with st.spinner("Writing caption..."):
                client = ai_client()
                prompt = f"""Write a {sm_platform} caption for a real estate {sm_type} post.

Details: {sm_details}
Agent: {agent_name} | {agent_company}
{'Include relevant hashtags' if sm_hashtags else 'No hashtags'}
{'Use emojis naturally' if sm_emoji else 'No emojis'}

Make it engaging, authentic, and professional.
{'Write separate versions for Instagram and Facebook.' if sm_platform=='Both' else ''}
Write ONLY the caption(s)."""
                try:
                    resp = client.messages.create(model="claude-sonnet-4-20250514",
                        max_tokens=400, messages=[{"role":"user","content":prompt}])
                    result = resp.content[0].text.strip()
                    st.markdown(f'<div class="mcard"><div class="lname">📱 {sm_type} Caption</div>'
                                f'<div class="mtxt">{result}</div></div>', unsafe_allow_html=True)
                    st.download_button("📥 Copy Caption", result,
                                        "caption.txt","text/plain", key="sm_dl")
                except Exception as e:
                    st.error(f"Error: {e}")

    # ── Appointment Setter ────────────────────────────────────────────────────
    with tt5:
        st.markdown("#### Appointment Setter")
        st.markdown('<div class="tip">Generate messages specifically designed to book a showing '
                    'or consultation call.</div>', unsafe_allow_html=True)
        c1,c2 = st.columns(2)
        with c1:
            ap_name     = st.text_input("Lead name", placeholder="John Smith", key="ap_name")
            ap_property = st.text_input("Property", placeholder="3/2 in Coral Gables", key="ap_prop")
            ap_type     = st.selectbox("Appointment type",
                                        ["Property Showing","Consultation Call",
                                         "Virtual Tour","Open House Invite"], key="ap_type")
        with c2:
            ap_dates    = st.text_input("Available times",
                                         placeholder="Saturday 2-5pm or Sunday anytime", key="ap_dates")
            ap_urgency  = st.selectbox("Urgency level",
                                        ["Low — casual invite","Medium — limited availability",
                                         "High — selling fast"], key="ap_urgency")
            ap_msg_type = st.selectbox("Message type",
                                        ["Text Message","Email","Phone Script"], key="ap_mtype")
        if st.button("📅 Generate Appointment Message", key="ap_gen"):
            with st.spinner("Writing..."):
                client = ai_client()
                prompt = f"""Write a {ap_msg_type} to book a {ap_type} with a real estate lead.

Agent: {agent_name} | {agent_phone} | {agent_company}
Lead: {ap_name}
Property: {ap_property}
Available times: {ap_dates}
Urgency: {ap_urgency}

Make it easy to say yes. Include a specific call to action with the times.
Natural and conversational, not pushy.
Write ONLY the message."""
                try:
                    resp = client.messages.create(model="claude-sonnet-4-20250514",
                        max_tokens=300, messages=[{"role":"user","content":prompt}])
                    result = resp.content[0].text.strip()
                    st.markdown(f'<div class="mcard"><div class="lname">📅 {ap_type} Message</div>'
                                f'<div class="mtxt">{result}</div></div>', unsafe_allow_html=True)
                    if ap_msg_type == "Text Message" and ap_name:
                        st.download_button("📥 Save", result, "appointment.txt",
                                            "text/plain", key="ap_dl")
                except Exception as e:
                    st.error(f"Error: {e}")

    # ── Cold Outreach ─────────────────────────────────────────────────────────
    with tt6:
        st.markdown("#### Cold Outreach Generator")
        st.markdown('<div class="tip">Generate first-touch messages for brand new leads '
                    'who have never heard from you before.</div>', unsafe_allow_html=True)

        co_file = st.file_uploader("Upload cold leads CSV", type=["csv","xlsx"],
                                    key="co_file")
        if co_file:
            co_df = pd.read_csv(co_file) if co_file.name.endswith(".csv")                     else pd.read_excel(co_file)
            st.success(f"✅ {len(co_df)} cold leads loaded")
            st.dataframe(co_df.head(5), use_container_width=True)

        st.markdown("**Or generate for one lead:**")
        c1,c2 = st.columns(2)
        with c1:
            co_name   = st.text_input("Name", placeholder="Bob Chen", key="co_name")
            co_source = st.selectbox("How you got their info",
                                      ["Zillow inquiry","Open house sign-in",
                                       "Referral","Social media","Door knock",
                                       "Direct mail responded"], key="co_source")
        with c2:
            co_area   = st.text_input("Area they're interested in",
                                       placeholder="Aventura, FL", key="co_area")
            co_type   = st.selectbox("Message type",
                                      ["Text Message","Email","DM"], key="co_type")

        if st.button("🥶 Generate Cold Outreach", key="co_gen"):
            with st.spinner("Writing..."):
                client = ai_client()
                prompt = f"""Write a first-touch cold outreach {co_type} to a real estate lead.

Agent: {agent_name} | {agent_phone} | {agent_company}
Lead name: {co_name}
How we got their info: {co_source}
Area of interest: {co_area}

This is the FIRST time contacting them. Be:
- Non-pushy and genuinely helpful
- Specific about why you're reaching out
- Brief — respect their time
- End with a low-commitment question or offer

Write ONLY the message."""
                try:
                    resp = client.messages.create(model="claude-sonnet-4-20250514",
                        max_tokens=250, messages=[{"role":"user","content":prompt}])
                    result = resp.content[0].text.strip()
                    st.markdown(f'<div class="mcard"><div class="lname">🥶 Cold Outreach for {co_name}</div>'
                                f'<div class="mtxt">{result}</div></div>', unsafe_allow_html=True)
                    st.download_button("📥 Save", result, "cold_outreach.txt",
                                        "text/plain", key="co_dl")
                except Exception as e:
                    st.error(f"Error: {e}")

# ══ TAB 4: ANALYTICS ══════════════════════════════════════════════════════════
with t4:
    st.markdown("### Analytics Dashboard")
    camps_data = get_user_campaigns(user["id"])
    msgs_data  = get_user_messages(user["id"])

    if not camps_data:
        st.info("No data yet. Generate some messages first.")
    else:
        total  = len(msgs_data)
        resp   = sum(1 for m in msgs_data if m.get("responded"))
        sent   = sum(1 for m in msgs_data if m.get("sent"))
        avg_sc = sum(m.get("lead_score",0) or 0 for m in msgs_data) / total if total else 0

        k1,k2,k3,k4 = st.columns(4)
        for col,n,l in [(k1,len(camps_data),"Campaigns"),(k2,total,"Messages"),
                         (k3,f"{resp/total:.0%}" if total else "—","Response Rate"),
                         (k4,f"{avg_sc:.1f}/10","Avg Lead Score")]:
            col.markdown(f'<div class="kpi"><div class="kpi-n">{n}</div>'
                         f'<div class="kpi-l">{l}</div></div>', unsafe_allow_html=True)
        st.markdown("<br>",unsafe_allow_html=True)

        st.markdown("#### Recent Campaigns")
        cdf = pd.DataFrame(camps_data)
        if not cdf.empty:
            show_cols = [c for c in ["created_at","total_leads","message_type","tone",
                                      "sequence_on","ab_on"] if c in cdf.columns]
            cdf["created_at"] = cdf["created_at"].str[:16].str.replace("T"," ")
            st.dataframe(cdf[show_cols], use_container_width=True)

        st.markdown("#### Message Log")
        mdf = pd.DataFrame(msgs_data)
        if not mdf.empty:
            show = [c for c in ["id","lead_name","seq_day","variant","lead_score",
                                  "sent","responded","created_at"] if c in mdf.columns]
            st.dataframe(mdf[show].head(50), use_container_width=True)

            c1,c2 = st.columns(2)
            with c1:
                mid_r = st.number_input("Mark ID as responded:", min_value=1, step=1, key="mark_r")
                if st.button("✅ Mark Responded"):
                    mark_responded(int(mid_r)); st.success("Marked!"); st.rerun()
            with c2:
                mid_s = st.number_input("Mark ID as sent:", min_value=1, step=1, key="mark_s")
                if st.button("📧 Mark Sent"):
                    mark_sent(int(mid_s)); st.success("Marked!"); st.rerun()

        # Score distribution
        scores = [m.get("lead_score",0) or 0 for m in msgs_data if m.get("lead_score")]
        if scores:
            st.markdown("#### Lead Score Distribution")
            score_counts = {str(i): scores.count(i) for i in range(1,11)}
            st.bar_chart(pd.DataFrame(list(score_counts.items()),
                          columns=["Score","Count"]).set_index("Score"))

# ══ ROI TRACKER ══════════════════════════════════════════════════════════════
with t_roi:
    st.markdown("### ROI Tracker")
    st.markdown('<div class="tip">Track which leads turned into closings and see your '
                'estimated commission value from RealFollow AI campaigns.</div>',
                unsafe_allow_html=True)

    # Log ROI event
    st.markdown("#### Log a Win")
    c1,c2,c3 = st.columns(3)
    with c1:
        roi_lead  = st.text_input("Lead name", placeholder="John Smith", key="roi_lead")
    with c2:
        roi_event = st.selectbox("Event type",
                                  ["Responded to message","Booked showing",
                                   "Made offer","Closed deal"], key="roi_event")
    with c3:
        roi_comm  = st.number_input("Est. commission ($)", min_value=0.0,
                                     step=500.0, key="roi_comm")
    if st.button("💰 Log Win", key="roi_log"):
        if roi_lead:
            save_roi_event(user["id"], roi_lead, roi_event, roi_comm)
            st.success(f"Win logged! +${roi_comm:,.0f} commission")
            st.rerun()

    # ROI Summary
    roi_events = get_roi_events(user["id"])
    if roi_events:
        total_comm  = sum(r.get("commission_est",0) for r in roi_events)
        closed      = sum(1 for r in roi_events if r.get("event_type")=="Closed deal")
        responded   = sum(1 for r in roi_events if "Responded" in r.get("event_type",""))
        showings    = sum(1 for r in roi_events if "showing" in r.get("event_type","").lower())

        st.markdown("---")
        k1,k2,k3,k4 = st.columns(4)
        for col,n,l in [
            (k1, f"${total_comm:,.0f}", "Total Commission Pipeline"),
            (k2, closed,   "Deals Closed"),
            (k3, showings, "Showings Booked"),
            (k4, responded,"Leads Responded"),
        ]:
            col.markdown(f'<div class="kpi"><div class="kpi-n">{n}</div>'
                         f'<div class="kpi-l">{l}</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("#### Win History")
        roi_df = pd.DataFrame(roi_events)[
            ["created_at","lead_name","event_type","commission_est"]]
        roi_df.columns = ["Date","Lead","Event","Commission"]
        roi_df["Date"] = roi_df["Date"].str[:10]
        roi_df["Commission"] = roi_df["Commission"].apply(lambda x: f"${x:,.0f}")
        st.dataframe(roi_df, use_container_width=True)

        # ROI vs cost
        plan_cost = {"starter":97,"professional":197,"team":397}.get(
            user.get("plan","starter"), 97)
        roi_multiple = total_comm / plan_cost if plan_cost > 0 else 0
        st.markdown(f"""<div class="ok">
        💰 You've generated <b>${total_comm:,.0f}</b> in commission pipeline
        from a ${plan_cost}/month investment — that's a
        <b>{roi_multiple:.0f}x return</b> on your RealFollow AI subscription.
        </div>""", unsafe_allow_html=True)
    else:
        st.info("No wins logged yet. Start logging responses and closings to track your ROI.")


# ══ DRIP CAMPAIGNS ═══════════════════════════════════════════════════════════
with t_drip:
    st.markdown("### Drip Campaign Builder")
    st.markdown('<div class="tip">Set up automated 30-day follow-up sequences. '
                'The app tracks when each message is due and reminds you to send it.</div>',
                unsafe_allow_html=True)

    with st.expander("➕ Create New Drip Campaign"):
        c1,c2 = st.columns(2)
        with c1:
            dr_name   = st.text_input("Campaign name", placeholder="Q1 Buyer Leads", key="dr_name")
            dr_lead   = st.text_input("Lead name",     placeholder="John Smith",      key="dr_lead")
            dr_email  = st.text_input("Lead email",    placeholder="john@email.com",  key="dr_email")
        with c2:
            dr_phone  = st.text_input("Lead phone",    placeholder="305-555-0101",    key="dr_phone")
            dr_prop   = st.text_input("Property interest", placeholder="3BR Miami",  key="dr_prop")
            dr_budget = st.text_input("Budget",         placeholder="$500k",          key="dr_budget")

        dr_days = st.multiselect("Send messages on days",
                                  [1,3,7,14,21,30], default=[1,3,7,14], key="dr_days")

        if st.button("🚀 Generate Drip Campaign", key="dr_gen"):
            if not dr_lead:
                st.error("Enter lead name.")
            else:
                with st.spinner(f"Building {len(dr_days)}-message drip campaign..."):
                    client = ai_client()
                    agent_info = {"name":agent_name,"phone":agent_phone,"company":agent_company}
                    lead_info  = {"Name":dr_lead,"Email":dr_email,"Phone":dr_phone,
                                  "Property":dr_prop,"Budget":dr_budget}
                    messages_dict = {}
                    schedule_dict = {}
                    for day in dr_days:
                        day_ctx = {
                            1:  "First follow-up — warm, reference their interest",
                            3:  "Day 3 — check in, offer specific value",
                            7:  "Week 1 — market update relevant to their search",
                            14: "Week 2 — new listings or price drops in their range",
                            21: "Week 3 — social proof, share a success story",
                            30: "Day 30 — final check-in, keep door open",
                        }.get(day, f"Day {day} follow-up")
                        prompt = f"""Write a Day {day} follow-up text message for a real estate lead.
Agent: {agent_name} | {agent_phone} | {agent_company}
Lead: {dr_lead} | Property: {dr_prop} | Budget: {dr_budget}
Context: {day_ctx}
Natural, personal, under 160 chars. Write ONLY the message."""
                        try:
                            resp = client.messages.create(
                                model="claude-sonnet-4-20250514", max_tokens=150,
                                messages=[{"role":"user","content":prompt}])
                            messages_dict[f"day_{day}"] = resp.content[0].text.strip()
                            schedule_dict[f"day_{day}"] = (
                                datetime.now() + timedelta(days=day)).isoformat()
                        except Exception as e:
                            messages_dict[f"day_{day}"] = f"Error: {e}"
                        time.sleep(0.3)

                    save_drip(user["id"], dr_name or f"{dr_lead} Drip",
                              dr_lead, dr_email, dr_phone,
                              messages_dict, schedule_dict)
                    st.success(f"✅ {len(dr_days)}-message drip campaign created for {dr_lead}!")
                    for day in dr_days:
                        msg = messages_dict.get(f"day_{day}","")
                        send_date = (datetime.now()+timedelta(days=day)).strftime("%b %d")
                        st.markdown(
                            f'<div class="mcard"><span class="dbadge">Day {day} — {send_date}</span>'
                            f'<div class="mtxt">{msg}</div></div>', unsafe_allow_html=True)
                    st.rerun()

    # Active drips
    drips = get_drips(user["id"])
    if drips:
        st.markdown(f"### Active Campaigns ({len(drips)})")
        for d in drips:
            msgs = json.loads(d.get("messages","{}"))
            sched = json.loads(d.get("schedule","{}"))
            with st.expander(f"📅 {d.get('name','')} — {d.get('lead_name','')} "
                             f"({len(msgs)} messages)"):
                st.markdown(f"**Lead:** {d.get('lead_name','')} | "
                             f"**Email:** {d.get('lead_email','')} | "
                             f"**Phone:** {d.get('lead_phone','')}")
                for key, msg in msgs.items():
                    day_num = key.replace("day_","")
                    send_dt = sched.get(key,"")[:10] if sched.get(key) else ""
                    st.markdown(
                        f'<div class="mcard"><span class="dbadge">Day {day_num}'
                        f'{f" — {send_dt}" if send_dt else ""}</span>'
                        f'<div class="mtxt">{msg}</div></div>',
                        unsafe_allow_html=True)
    else:
        st.info("No drip campaigns yet. Create one above.")


# ══ SMS INBOX ═════════════════════════════════════════════════════════════════
with t_sms:
    st.markdown("### SMS Inbox")
    st.markdown('<div class="tip">Two-way SMS inbox. Requires Twilio setup in your config '
                '(SMTP_EMAIL and TWILIO credentials). Log incoming replies manually below '
                'until Twilio is connected.</div>', unsafe_allow_html=True)

    # Manual reply logging
    with st.expander("📥 Log an Incoming Reply"):
        c1,c2 = st.columns(2)
        with c1:
            sms_from = st.text_input("From (phone or name)", placeholder="305-555-0101", key="sms_from")
            sms_lead = st.text_input("Lead name", placeholder="John Smith", key="sms_lead")
        with c2:
            sms_msg  = st.text_area("Their message", height=80, key="sms_msg")
        if st.button("📥 Log Reply", key="sms_log"):
            if sms_msg:
                c2db = sqlite3.connect(DB)
                c2db.execute("INSERT INTO sms_inbox VALUES(NULL,?,?,?,?,?,?,0)",
                             (user["id"], sms_from, sms_lead, sms_msg,
                              "inbound", datetime.now().isoformat()))
                c2db.commit(); c2db.close()
                st.success("Reply logged!")
                # Auto-mark lead as responded
                c3db = sqlite3.connect(DB)
                c3db.execute(
                    "UPDATE messages SET responded=1 WHERE user_id=? AND lead_name=?",
                    (user["id"], sms_lead))
                c3db.commit(); c3db.close()
                st.rerun()

    # Inbox
    sms_messages = get_sms_inbox(user["id"])
    if sms_messages:
        unread = sum(1 for m in sms_messages if not m.get("read_integer"))
        st.markdown(f"#### Inbox ({len(sms_messages)} messages, {unread} unread)")
        for m in sms_messages:
            bg = "#1a2535" if m.get("read_integer") else "#1a3a2a"
            direction = "📥" if m.get("direction")=="inbound" else "📤"
            st.markdown(
                f'<div style="background:{bg};border:1px solid #2d3561;'
                f'border-radius:10px;padding:14px;margin-bottom:10px">'
                f'<div style="color:#6c8aff;font-weight:700;margin-bottom:6px">'
                f'{direction} {m.get("lead_name","")} — {m.get("from_number","")}'
                f'<span style="color:#718096;font-size:12px;margin-left:12px">'
                f'{m.get("created_at","")[:16].replace("T"," ")}</span></div>'
                f'<div style="color:#e2e8f0">{m.get("message","")}</div></div>',
                unsafe_allow_html=True)

        # Quick reply
        st.markdown("#### Quick Reply")
        reply_to = st.selectbox("Reply to",
                                 [m.get("lead_name","") for m in sms_messages
                                  if m.get("direction")=="inbound"], key="sms_reply_to")
        reply_msg = st.text_area("Your reply", height=80, key="sms_reply_msg")
        if st.button("📤 Send Reply", key="sms_send"):
            if reply_msg and SMTP_EMAIL:
                c2db = sqlite3.connect(DB)
                c2db.execute("INSERT INTO sms_inbox VALUES(NULL,?,?,?,?,?,?,1)",
                             (user["id"], "agent", reply_to, reply_msg,
                              "outbound", datetime.now().isoformat()))
                c2db.commit(); c2db.close()
                st.success(f"Reply sent to {reply_to}!")
                st.rerun()
            else:
                st.info("Log your reply — connect Twilio to send automatically.")
    else:
        st.info("No messages yet. Log incoming replies above.")


# ══ TAB 5: CRM EXPORT ════════════════════════════════════════════════════════
with t5:
    st.markdown("### CRM Export")
    msgs_data = get_user_messages(user["id"])
    if not msgs_data:
        st.info("Generate messages first.")
    else:
        c1,c2 = st.columns(2)
        with c1:
            st.markdown("#### 📋 Follow Up Boss")
            fub = [{"Contact Name":m.get("lead_name",""),
                    "Email":m.get("lead_email",""),
                    "Note":m.get("message",""),
                    "Note Type":f"Day {m.get('seq_day',1)} Version {m.get('variant','A')}",
                    "Lead Score":m.get("lead_score",""),
                    "Created":m.get("created_at","")[:10]} for m in msgs_data]
            st.dataframe(pd.DataFrame(fub).head(10), use_container_width=True)
            st.download_button("📥 Follow Up Boss CSV",
                pd.DataFrame(fub).to_csv(index=False),"followupboss.csv","text/csv")
        with c2:
            st.markdown("#### 🏠 KVCore")
            kv = [{"Lead Name":m.get("lead_name",""),
                   "Email":m.get("lead_email",""),
                   "Task":"Follow Up",
                   "Due Date":(datetime.now()+timedelta(
                       days=m.get("seq_day",1))).strftime("%Y-%m-%d"),
                   "Notes":m.get("message",""),
                   "Priority":"High" if m.get("seq_day",1)==1 else "Normal",
                   "Score":m.get("lead_score","")} for m in msgs_data]
            st.dataframe(pd.DataFrame(kv).head(10), use_container_width=True)
            st.download_button("📥 KVCore CSV",
                pd.DataFrame(kv).to_csv(index=False),"kvcore.csv","text/csv")

        st.markdown("---")
        st.markdown("#### Plain Text")
        fd = st.selectbox("Filter", ["All","Day 1","Day 3","Day 7"])
        filtered = msgs_data if fd=="All" else \
                   [m for m in msgs_data if m.get("seq_day")==int(fd.split()[1])]
        plain = "\n\n".join([f"LEAD: {m['lead_name']}\nDay {m.get('seq_day',1)} "
                              f"Version {m.get('variant','A')}\n{'='*40}\n{m['message']}"
                              for m in filtered])
        st.text_area("Messages", plain, height=250)
        st.download_button("📥 Plain Text", plain, "messages.txt","text/plain")

# ══ TAB: SCRIPT LIBRARY ══════════════════════════════════════════════════════
with t_scripts:
    st.markdown("### Script Library")
    st.markdown('<div class="tip">Your saved best-performing scripts. '
                'Build your library as you use the tools.</div>', unsafe_allow_html=True)

    # Init scripts table
    c2 = sqlite3.connect(DB)
    c2.execute("CREATE TABLE IF NOT EXISTS scripts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
               "user_id INTEGER, category TEXT, title TEXT, content TEXT, created_at TEXT)")
    c2.commit()
    c2.row_factory = sqlite3.Row
    scripts = [dict(r) for r in c2.execute(
        "SELECT * FROM scripts WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],)).fetchall()]
    c2.close()

    # Add custom script
    with st.expander("➕ Add Script Manually"):
        sc_title   = st.text_input("Title", placeholder="My best follow-up opener", key="sc_title")
        sc_cat     = st.selectbox("Category", ["follow_up","objection","cold_outreach",
                                                "appointment","social","other"], key="sc_cat")
        sc_content = st.text_area("Script content", height=120, key="sc_content")
        if st.button("💾 Save Script", key="sc_save"):
            if sc_title and sc_content:
                c2 = sqlite3.connect(DB)
                c2.execute("INSERT INTO scripts VALUES(NULL,?,?,?,?,?)",
                           (user["id"], sc_cat, sc_title, sc_content,
                            datetime.now().isoformat()))
                c2.commit(); c2.close()
                st.success("Saved!"); st.rerun()

    if not scripts:
        st.info("No scripts saved yet. Use the AI Tools tab and save responses you like.")
    else:
        # Filter by category
        cats = list(set(s.get("category","other") for s in scripts))
        sel_cat = st.selectbox("Filter by category", ["All"] + cats, key="sl_cat")
        filtered = scripts if sel_cat=="All" else                    [s for s in scripts if s.get("category")==sel_cat]

        st.markdown(f"**{len(filtered)} scripts**")
        for s in filtered:
            with st.expander(f"📝 {s.get('title','')} — {s.get('category','')}"):
                st.text_area("", s.get("content",""), height=100,
                              key=f"sc_{s['id']}")
                col1, col2 = st.columns(2)
                with col1:
                    st.download_button("📥 Download", s.get("content",""),
                                        f"script_{s['id']}.txt","text/plain",
                                        key=f"dl_{s['id']}")
                with col2:
                    if st.button("🗑️ Delete", key=f"del_{s['id']}"):
                        c2 = sqlite3.connect(DB)
                        c2.execute("DELETE FROM scripts WHERE id=?", (s["id"],))
                        c2.commit(); c2.close()
                        st.rerun()

# ══ TAB 6: MARKET REPORT ══════════════════════════════════════════════════════
with t6:
    st.markdown("### Market Report Generator")
    st.markdown('<div class="tip">Generate a professional market report to send alongside '
                'your follow-up messages. Great for warming up cold leads.</div>',
                unsafe_allow_html=True)

    c1,c2 = st.columns(2)
    with c1:
        report_city = st.text_input("City/Market", placeholder="Miami, FL")
    with c2:
        report_lead = st.text_input("Send to lead (optional)", placeholder="John Smith")

    if st.button("📄 Generate Market Report"):
        if not report_city:
            st.error("Enter a city.")
        else:
            with st.spinner("Generating report..."):
                client     = ai_client()
                agent_info = {"name":agent_name,"phone":agent_phone,"company":agent_company}
                report     = generate_market_report(client, report_city, agent_info)

            st.markdown("### Your Market Report")
            st.text_area("Report", report, height=400)

            c1e,c2e = st.columns(2)
            with c1e:
                st.download_button("📥 Download Report", report,
                    f"market_report_{report_city.replace(' ','_')}.txt","text/plain")
            with c2e:
                if report_lead and SMTP_EMAIL:
                    email_to = st.text_input("Lead's email", placeholder="john@email.com",
                                              key="report_email")
                    if st.button("📧 Email Report to Lead"):
                        ok, msg = send_email(
                            email_to,
                            f"Real Estate Market Update — {report_city}",
                            report, agent_name)
                        st.success("Sent!") if ok else st.error(msg)

# ══ TAB 7: BRANDING ═══════════════════════════════════════════════════════════
with t7:
    st.markdown("### Custom Branding")
    st.markdown("Customize how the app looks for your clients.")

    c1,c2 = st.columns(2)
    with c1:
        new_color    = st.color_picker("Brand Color", value=brand_color)
        logo_url     = st.text_input("Logo URL (optional)",
                                      value=user.get("logo_url","") or "",
                                      placeholder="https://yoursite.com/logo.png")
        app_name     = st.text_input("App Name (White Label)",
                                      value=user.get("app_name","RealFollow AI") or "RealFollow AI",
                                      placeholder="Your Company AI")
        custom_domain = st.text_input("Custom Domain (optional)",
                                       placeholder="app.yourcompany.com",
                                       help="Contact us to set up a custom domain")
        if st.button("💾 Save Branding"):
            update_user(user["id"], brand_color=new_color, logo_url=logo_url)
            st.session_state.user = get_user_by_id(user["id"])
            st.success("Branding saved! Refresh to see changes.")
            st.rerun()
    with c2:
        st.markdown("**Preview:**")
        st.markdown(
            f'<div style="background:linear-gradient(135deg,#1a1f36,#2d3561);'
            f'padding:24px;border-radius:12px;border:1px solid {new_color}">'
            f'<span style="font-size:24px;font-weight:800;color:#fff">🏡 {app_name} '
            f'<span style="color:{new_color}">·</span></span><br>'
            f'<span style="color:#a0aec0;font-size:14px">Your brand · Your colors · Your name</span>'
            f'</div>', unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="tip">🏷️ <b>White Label:</b> Agents on the Team plan see '
                    'your app name instead of "RealFollow AI". Perfect for reselling '
                    'to your own clients under your brand.</div>', unsafe_allow_html=True)

# ══ TAB 8: BILLING ════════════════════════════════════════════════════════════
with t8:
    st.markdown("### Billing & Subscription")
    current_plan = user.get("plan","starter")

    st.markdown(f'<div style="background:#1a1f2e;border:2px solid {brand_color};'
                f'border-radius:12px;padding:20px;margin-bottom:24px">'
                f'<span style="color:{brand_color};font-weight:700;font-size:16px">'
                f'Current Plan: {current_plan.upper()}</span><br>'
                f'<span style="color:#a0aec0">Manage your subscription below.</span>'
                f'</div>', unsafe_allow_html=True)

    c1,c2,c3 = st.columns(3)
    for col, plan_key in [(c1,"starter"),(c2,"professional"),(c3,"team")]:
        p = PLANS[plan_key]
        border = brand_color if plan_key==current_plan else "#2d3561"
        badge  = (f'<div style="position:absolute;top:-12px;left:50%;transform:translateX(-50%);'
                  f'background:{brand_color};color:white;padding:3px 14px;border-radius:20px;'
                  f'font-size:11px;font-weight:700">CURRENT</div>') if plan_key==current_plan else ""
        feats = "".join([f"<div style='color:#a0aec0;font-size:13px;padding:3px 0'>✓ {f}</div>"
                          for f in p["features"]])
        col.markdown(
            f'<div style="background:#1a1f2e;border:2px solid {border};border-radius:16px;'
            f'padding:28px;text-align:center;position:relative">{badge}'
            f'<div style="font-size:18px;font-weight:700;color:#fff;margin-bottom:8px">{p["name"]}</div>'
            f'<div style="font-size:32px;font-weight:800;color:{brand_color}">${p["price"]}</div>'
            f'<div style="color:#718096;font-size:12px;margin-bottom:16px">/month</div>'
            f'{feats}</div>', unsafe_allow_html=True)

        if plan_key != current_plan:
            if col.button(f"Upgrade to {p['name']}", key=f"plan_{plan_key}"):
                if STRIPE_KEY:
                    st.info(f"Stripe integration: redirect to checkout for {p['name']} at ${p['price']}/mo")
                    # In production: create Stripe checkout session here
                else:
                    # For demo: just upgrade directly
                    update_user(user["id"], plan=plan_key)
                    st.session_state.user = get_user_by_id(user["id"])
                    st.success(f"Upgraded to {p['name']}!")
                    st.rerun()

    st.markdown("---")
    st.markdown("#### Payment History")
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    payments = [dict(r) for r in c.execute(
        "SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],)).fetchall()]
    c.close()
    if payments:
        st.dataframe(pd.DataFrame(payments), use_container_width=True)
    else:
        st.info("No payment history yet.")

# ══ TAB 9: HOW IT WORKS ════════════════════════════════════════════════════════
with t9:
    st.markdown("### Everything RealFollow AI Can Do")
    st.markdown('<div class="tip">13 tools built specifically for real estate agents — '
                'all powered by Claude AI, all in one place.</div>', unsafe_allow_html=True)

    st.markdown("#### 📤 Lead Follow-Up (The Core)")
    for i,(title,desc) in enumerate([
        ("Bulk Upload & Generate","Upload any CSV of leads. AI writes personalized follow-up messages for every single one — referencing their specific property, budget, and situation."),
        ("3-Day Sequences","Every lead gets Day 1, Day 3, and Day 7 messages automatically. The right message at the right time without you having to think about it."),
        ("A/B Variants","Two different styles per message — Version A is direct, Version B leads with a market hook. Pick whichever feels right."),
        ("Lead Scoring","Every lead gets a 1-10 score with reasoning. Work your hottest leads first."),
        ("Send Directly","Hit Send right from the app. No copy/paste, no switching between tools."),
        ("Voicemail Scripts","Generate 30-45 second voicemail scripts alongside every text/email."),
    ], 1):
        st.markdown(
            f'<div style="display:flex;align-items:flex-start;margin-bottom:12px;'
            f'background:#1a1f2e;padding:16px;border-radius:10px;border:1px solid #2d3561">'
            f'<div style="background:{brand_color};color:white;border-radius:50%;'
            f'width:28px;height:28px;display:flex;align-items:center;justify-content:center;'
            f'font-weight:800;font-size:13px;flex-shrink:0;margin-right:12px">{i}</div>'
            f'<div><div style="font-weight:700;color:#fff;margin-bottom:3px">{title}</div>'
            f'<div style="color:#a0aec0;font-size:13px">{desc}</div></div></div>',
            unsafe_allow_html=True)

    st.markdown("#### 🛠️ AI Tools")
    for title,desc in [
        ("Objection Handler","Get the perfect response to any objection — 'I'm not ready', 'working with another agent', 'rates are too high'. Pick from common objections or type your own."),
        ("Appointment Setter","Messages specifically designed to book showings or consultation calls. Set urgency level and available times."),
        ("Cold Outreach","First-touch messages for brand new leads who've never heard from you. Works for Zillow inquiries, open house sign-ins, referrals, door knocks."),
        ("Neighborhood Reports","Full professional neighborhood breakdowns — schools, safety, property values, commute, restaurants. Email directly to leads."),
        ("Social Media Captions","Instagram and Facebook posts for new listings, just sold, open houses, market updates. With hashtags and emojis."),
        ("Property Suggestions","Describe what a lead wants, get 3 AI-generated property suggestions with pitches ready to paste into your message."),
        ("Market Reports","Full market reports for any city — median prices, days on market, buyer/seller assessment, key talking points. Email to leads."),
    ]:
        st.markdown(
            f'<div style="display:flex;align-items:flex-start;margin-bottom:10px;'
            f'background:#1a1f2e;padding:14px;border-radius:10px;border:1px solid #2d3561">'
            f'<div style="color:{brand_color};font-size:18px;margin-right:12px;flex-shrink:0">→</div>'
            f'<div><div style="font-weight:700;color:#fff;margin-bottom:2px">{title}</div>'
            f'<div style="color:#a0aec0;font-size:13px">{desc}</div></div></div>',
            unsafe_allow_html=True)

    st.markdown("#### 📊 Automation & Tracking")
    for title,desc in [
        ("Drip Campaigns","Set up 30-day automated sequences. Pick which days to send, AI generates all messages at once with the right tone for each day."),
        ("ROI Tracker","Log every win — responses, showings, offers, closings. See your total commission pipeline and exact ROI from your subscription."),
        ("SMS Inbox","Two-way message log. Track all conversations in one place. Responds auto-mark leads as converted."),
        ("Script Library","Every good response you generate gets saved automatically. Build your personal playbook of what works."),
        ("Analytics Dashboard","Response rates, lead scores, best-performing campaigns, tone breakdown. Know exactly what's working."),
        ("Weekly Report","One click generates a full weekly performance summary. Email it to yourself every Monday."),
        ("CRM Export","Download everything in Follow Up Boss or KVCore format. Or plain text for any other CRM."),
    ]:
        st.markdown(
            f'<div style="display:flex;align-items:flex-start;margin-bottom:10px;'
            f'background:#1a1f2e;padding:14px;border-radius:10px;border:1px solid #2d3561">'
            f'<div style="color:{brand_color};font-size:18px;margin-right:12px;flex-shrink:0">→</div>'
            f'<div><div style="font-weight:700;color:#fff;margin-bottom:2px">{title}</div>'
            f'<div style="color:#a0aec0;font-size:13px">{desc}</div></div></div>',
            unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### 💰 Pricing")
    for i,(title,desc) in enumerate([
        ("Upload Your Leads","x"),
    ], 1):
        st.markdown(
            f'<div style="display:flex;align-items:flex-start;margin-bottom:16px;'
            f'background:#1a1f2e;padding:18px;border-radius:12px;border:1px solid #2d3561">'
            f'<div style="background:{brand_color};color:white;border-radius:50%;'
            f'width:32px;height:32px;display:flex;align-items:center;justify-content:center;'
            f'font-weight:800;font-size:14px;flex-shrink:0;margin-right:14px">{i}</div>'
            f'<div><div style="font-weight:700;color:#fff;margin-bottom:4px">{title}</div>'
            f'<div style="color:#a0aec0;font-size:14px">{desc}</div></div></div>',
            unsafe_allow_html=True)

st.markdown(f'<br><div style="text-align:center;color:#4a5568;font-size:12px;'
            f'padding:16px;border-top:1px solid #2d3561">'
            f'RealFollow AI · Powered by Claude AI · Built for Real Estate Professionals'
            f'</div>', unsafe_allow_html=True)
