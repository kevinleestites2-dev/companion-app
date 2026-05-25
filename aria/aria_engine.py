"""
Aria Engine — PropPilot AI Companion Core
==========================================
Fuses:
  1. companion-app persona engine (a16z architecture)
  2. ElevenLabs voice (turbo — low latency)
  3. ScoutPrime lead data (pre-call context injection)
  4. pgvector persistent memory (seller recall across calls)
  5. Twilio outbound dialer

Author: ZapiaPrime | Pantheon
"""

import os
import json
import asyncio
from datetime import datetime
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
ELEVEN_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel — warm female
TWILIO_SID      = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER   = os.getenv("TWILIO_PHONE_NUMBER", "")
DATABASE_URL    = os.getenv("DATABASE_URL", "")  # Supabase pgvector
STRIPE_LINK     = "https://buy.stripe.com/aFadR2fG22C02Fg5Ma8Ra00"

SCOUT_DATA_PATH   = Path("scout_v4_20260525_110610.json")
ARIA_PERSONA_PATH = Path("aria_proppilot.txt")


# ── Aria Persona ─────────────────────────────────────────────────────────────
def load_persona() -> str:
    if ARIA_PERSONA_PATH.exists():
        return ARIA_PERSONA_PATH.read_text()
    return "You are Aria, a warm real estate specialist at PropPilot AI."

ARIA_PERSONA = load_persona()


# ── ScoutPrime Lead Loader ────────────────────────────────────────────────────
def load_leads() -> list:
    if not SCOUT_DATA_PATH.exists():
        return []
    with open(SCOUT_DATA_PATH) as f:
        data = json.load(f)
    return data.get("all_leads", [])


def get_lead_context(address: str, leads: list) -> dict:
    address_lower = address.lower()
    for lead in leads:
        lead_addr = lead.get("address", "").lower()
        if any(part in lead_addr for part in address_lower.split() if len(part) > 4):
            return lead
    return {}


def format_lead_briefing(lead: dict) -> str:
    if not lead:
        return ""
    return (
        "\nPRE-CALL INTELLIGENCE (use naturally — do not reveal source):\n"
        f"- Property: {lead.get('address', 'Unknown')}\n"
        f"- Type: {lead.get('type', 'Unknown')}\n"
        f"- Auction Date: {lead.get('auction_date', 'Unknown')}\n"
        f"- Status: {lead.get('status', 'Unknown')}\n"
        f"- Last Sale Price: {lead.get('price', 'Unknown')}\n"
        f"- Size: {lead.get('beds','?')}bd/{lead.get('baths','?')}ba, {lead.get('sqft','?')} sqft\n"
        f"- Tier: {lead.get('tier', 'Unknown')}\n"
    )


# ── Memory Engine (pgvector + local fallback) ─────────────────────────────────
class AriaMemory:
    def __init__(self):
        self.local_path = Path("aria_memory_local.json")
        self.local = json.loads(self.local_path.read_text()) if self.local_path.exists() else {}
        self.db_available = bool(DATABASE_URL)

    def _save(self):
        self.local_path.write_text(json.dumps(self.local, indent=2))

    async def recall(self, seller_id: str) -> dict:
        if self.db_available:
            return await self._recall_pg(seller_id)
        return self.local.get(seller_id, {})

    async def remember(self, seller_id: str, facts: dict):
        existing = self.local.get(seller_id, {})
        existing.update(facts)
        existing["last_contact"] = datetime.now().isoformat()
        self.local[seller_id] = existing
        self._save()
        if self.db_available:
            await self._upsert_pg(seller_id, existing)
        print(f"[MEMORY] Saved {list(facts.keys())} for seller {seller_id}")

    async def _recall_pg(self, seller_id: str) -> dict:
        try:
            import asyncpg
            conn = await asyncpg.connect(DATABASE_URL)
            row = await conn.fetchrow(
                "SELECT facts FROM aria_seller_memory WHERE seller_id = $1", seller_id
            )
            await conn.close()
            return json.loads(row["facts"]) if row else {}
        except Exception as e:
            print(f"[MEMORY] pgvector recall failed, using local: {e}")
            return self.local.get(seller_id, {})

    async def _upsert_pg(self, seller_id: str, facts: dict):
        try:
            import asyncpg
            conn = await asyncpg.connect(DATABASE_URL)
            await conn.execute("""
                INSERT INTO aria_seller_memory (seller_id, facts, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (seller_id) DO UPDATE
                SET facts = $2, updated_at = NOW()
            """, seller_id, json.dumps(facts))
            await conn.close()
        except Exception as e:
            print(f"[MEMORY] pgvector upsert failed: {e}")


# SQL to run once in Supabase
PGVECTOR_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS aria_seller_memory (
    id         SERIAL PRIMARY KEY,
    seller_id  TEXT UNIQUE NOT NULL,
    facts      JSONB NOT NULL DEFAULT '{}',
    embedding  vector(1536),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_seller_id ON aria_seller_memory (seller_id);
"""


# ── Aria Brain (GPT-4o) ───────────────────────────────────────────────────────
class AriaBrain:
    def __init__(self, memory: AriaMemory):
        self.memory = memory
        self.leads = load_leads()
        print(f"[ARIA] Loaded {len(self.leads)} ScoutPrime leads into memory")

    def _build_system(self, recalled: dict, lead_briefing: str) -> str:
        memory_block = ""
        if recalled:
            memory_block = f"\nWHAT YOU ALREADY KNOW ABOUT THIS SELLER:\n{json.dumps(recalled, indent=2)}\n"
        return f"{ARIA_PERSONA}\n{memory_block}\n{lead_briefing}\nCONSULT BOOKING LINK: {STRIPE_LINK}"

    async def respond(self, seller_id: str, address: str, conversation: list) -> str:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=OPENAI_API_KEY)

            recalled = await self.memory.recall(seller_id)
            lead = get_lead_context(address, self.leads)
            briefing = format_lead_briefing(lead)
            system = self._build_system(recalled, briefing)

            messages = [{"role": "system", "content": system}] + conversation
            resp = await client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.85,
                max_tokens=300
            )
            reply = resp.choices[0].message.content.strip()
            await self._extract_facts(seller_id, conversation, reply)
            return reply

        except Exception as e:
            print(f"[BRAIN] Error: {e}")
            return "Sorry, I missed that. Can you say that again?"

    async def _extract_facts(self, seller_id: str, conversation: list, reply: str):
        facts = {}
        full = " ".join(m.get("content", "") for m in conversation).lower()
        if "auction" in full:
            facts["mentioned_auction"] = True
        if "tax" in full or "lien" in full:
            facts["has_tax_issue"] = True
        if "foreclosure" in full:
            facts["in_foreclosure"] = True
        if "consult" in reply.lower() or "20 minute" in reply.lower():
            facts["consult_offered"] = True
        if facts:
            await self.memory.remember(seller_id, facts)


# ── Voice Layer (ElevenLabs) ──────────────────────────────────────────────────
class AriaVoice:
    def __init__(self):
        self.voice_id = ELEVEN_VOICE_ID
        self.api_key  = ELEVEN_API_KEY

    async def speak(self, text: str) -> bytes:
        if not self.api_key:
            print(f"[VOICE] No ElevenLabs key — text only: {text[:60]}")
            return b""
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
                    headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                    json={
                        "text": text,
                        "model_id": "eleven_turbo_v2",
                        "voice_settings": {"stability": 0.5, "similarity_boost": 0.85}
                    },
                    timeout=10.0
                )
                return r.content
        except Exception as e:
            print(f"[VOICE] ElevenLabs error: {e}")
            return b""


# ── Twilio Dialer ─────────────────────────────────────────────────────────────
class AriaDialer:
    def __init__(self):
        self.sid    = TWILIO_SID
        self.token  = TWILIO_TOKEN
        self.from_  = TWILIO_NUMBER

    def call(self, to_number: str, webhook_url: str) -> str:
        if not self.sid:
            print(f"[DIALER] No Twilio — simulating call to {to_number}")
            return "SIMULATED"
        try:
            from twilio.rest import Client
            c = Client(self.sid, self.token)
            call = c.calls.create(to=to_number, from_=self.from_, url=webhook_url)
            print(f"[DIALER] Called {to_number} — SID: {call.sid}")
            return call.sid
        except Exception as e:
            print(f"[DIALER] Twilio error: {e}")
            return ""


# ── Full Aria Session ─────────────────────────────────────────────────────────
class AriaSession:
    def __init__(self):
        self.memory = AriaMemory()
        self.brain  = AriaBrain(self.memory)
        self.voice  = AriaVoice()
        self.dialer = AriaDialer()

    async def text_session(self, seller_id: str, address: str = ""):
        print(f"\n{'='*60}")
        print("ARIA — PropPilot AI | Text Mode (Dev)")
        print(f"Seller ID: {seller_id}  |  Address: {address or 'Unknown'}")
        print("Type 'quit' to end")
        print(f"{'='*60}\n")

        conversation = []
        opener = await self.brain.respond(seller_id, address, conversation)
        print(f"Aria: {opener}\n")
        conversation.append({"role": "assistant", "content": opener})

        while True:
            user_input = input("Seller: ").strip()
            if user_input.lower() in ["quit", "exit", "q"]:
                print("\n[SESSION ENDED]")
                break
            if not user_input:
                continue
            conversation.append({"role": "user", "content": user_input})
            reply = await self.brain.respond(seller_id, address, conversation)
            conversation.append({"role": "assistant", "content": reply})
            print(f"\nAria: {reply}\n")

    def run_campaign(self, webhook_url: str, tier: str = "Tier 1"):
        leads = [l for l in self.brain.leads if tier in l.get("tier", "")]
        print(f"[CAMPAIGN] Dialing {len(leads)} {tier} leads...")
        for lead in leads:
            phone = lead.get("phone", "")
            if phone:
                self.dialer.call(phone, webhook_url)
        print("[CAMPAIGN] All calls initiated.")


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    session = AriaSession()

    if "--campaign" in sys.argv:
        WEBHOOK = os.getenv("ARIA_WEBHOOK_URL", "https://propilot.ai/aria/voice")
        session.run_campaign(WEBHOOK, tier="Tier 1")
    else:
        asyncio.run(session.text_session(
            seller_id="test_seller_001",
            address="Leeland Heights Blvd"
        ))
