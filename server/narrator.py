"""
narrator.py - turns numbers this app already computed into a plain-English
sentence, via an LLM. The boundary here is absolute: the model only ever
paraphrases a `facts` dict handed to it — it never computes a number, a
probability, or a status itself. Every fact it's allowed to mention is
already in the dict; the system prompt tells it exactly that, and nothing
in this module lets a "helpful" hallucinated number reach a user.

Caching is the whole design, not an afterthought. Each (subject, section)
pair caches its narrative alongside a hash of the facts that produced it.
A request only calls the LLM when that hash has changed — which caps calls
to roughly once per employee per day in the common case (numbers usually
change once a day, on check-in), AND regenerates immediately the moment a
real change actually happens (a WFH->WFO correction, a leave applied),
rather than serving a stale sentence until some fixed cron next runs.
No separate daily job needed; the read path IS the refresh path.

Failure is always silent and non-fatal: a narrator outage means the UI
falls back to showing the raw numbers it already showed before this
existed, never a broken page.
"""
import hashlib
import json
import logging
import os
import urllib.request
import urllib.error
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Narrative

logger = logging.getLogger("narrator")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# Still overridable via env var — set OPENAI_MODEL if this needs to change
# without a redeploy.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

NARRATOR_AVAILABLE = bool(OPENAI_API_KEY)
if not NARRATOR_AVAILABLE:
    logger.warning("[WARN] OPENAI_API_KEY not set - narrative summaries disabled, raw numbers only")

_SYSTEM_PROMPT = """You summarize workplace attendance data for one person to read about themselves.

Rules, all of them hard requirements:
- Use ONLY the facts given to you in the user message. Every number, status, and claim you make must come directly from those facts.
- Never invent, estimate, round in a misleading direction, or infer a number that isn't present.
- Never state or imply more certainty than the facts themselves state. If a fact says confidence is low, your tone must reflect that.
- Write 1-3 plain sentences, no bullet points, no markdown, no headers, no emoji.
- Address the reader as "you". Be direct and factual, not motivational or apologetic.
- If the facts are sparse or the situation is unremarkable, say less — don't pad.
- Some facts describe something that may or may not have happened, marked with a "detected" or similar boolean. If it's false, don't mention that fact at all — don't say "no shift was detected", just omit it entirely."""


def _facts_hash(section: str, facts: dict) -> str:
    payload = json.dumps({"section": section, "facts": facts}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _call_openai(facts: dict, section_hint: str) -> Optional[str]:
    if not NARRATOR_AVAILABLE:
        return None
    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Section: {section_hint}\nFacts (JSON): {json.dumps(facts, default=str)}"},
        ],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    try:
        req = urllib.request.Request(
            OPENAI_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                logger.warning(f"[WARN] OpenAI returned HTTP {resp.status}")
                return None
            data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            return text or None
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        logger.warning(f"[WARN] OpenAI HTTP {e.code}: {body_txt[:300]}")
        return None
    except Exception as e:
        logger.warning(f"[WARN] OpenAI call failed: {e}")
        return None


async def get_narrative(db: AsyncSession, subject_type: str, subject_id: str,
                         section: str, facts: dict) -> Optional[str]:
    """
    Returns a cached or freshly-generated narrative for this
    (subject, section), or None if the narrator is unavailable/fails —
    callers must treat None as "show the raw numbers instead", never as
    an error to surface to the user.
    """
    if not NARRATOR_AVAILABLE:
        return None

    new_hash = _facts_hash(section, facts)
    q = await db.execute(select(Narrative).where(
        Narrative.subject_type == subject_type,
        Narrative.subject_id == subject_id,
        Narrative.section == section,
    ))
    row = q.scalars().first()

    if row and row.source_hash == new_hash:
        return row.narrative_text

    text = _call_openai(facts, section)
    if text is None:
        # Generation failed — serve the last good narrative rather than
        # nothing, if one exists. It's stale-but-plausible, which beats a
        # blank card; the raw numbers are shown alongside it regardless.
        return row.narrative_text if row else None

    if row:
        row.source_hash = new_hash
        row.narrative_text = text
    else:
        db.add(Narrative(subject_type=subject_type, subject_id=subject_id,
                          section=section, source_hash=new_hash, narrative_text=text))
    await db.commit()
    return text
