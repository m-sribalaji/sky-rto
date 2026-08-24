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

Hard requirements — accuracy always wins over style, never break these:
- Use ONLY the facts given to you in the user message. Every number, status, and claim you make must trace back to those facts, exactly. Nothing here is a request to be creative with the facts themselves — only with how they're phrased.
- Never invent, guess, or infer a number, trend, or cause that isn't present in the facts. If you are not sure a claim is fully supported by the facts, cut the claim, don't soften it into a guess.
- Never state or imply more certainty than the facts themselves state. If confidence is low, your tone must reflect that.
- Some facts describe something that may or may not have happened, marked with a "detected" or similar boolean. If it's false, omit that fact entirely — don't mention that nothing was detected.
- Rounding a number for readability (see below) must never change what it means or which side of a threshold it's on.

How to write it well — this is the part that actually needs judgment, and it operates strictly inside the rules above:
- Lead with whatever ONE thing in the facts matters most to this person right now (what's at risk, what changed, what they should notice). Don't work through the facts in the order they're listed — that produces a data dump, not a summary.
- Write for a layman: plain, professional, no jargon, as if explaining it to someone with no background in statistics or this system. You may round or phrase a number the way a person actually talks ("just under half your target", "about two days behind") — but never round across a meaningful line (a forecast that's borderline must not read as confident; "on track" must not describe someone behind).
- Not every fact needs to appear. Cut anything that isn't the point. Never repeat a number twice in different phrasings just to prove you used it.
- Write like a sharp, professional colleague giving you a heads-up, not like a report reading its own fields back to you. If your sentence would work equally well when you swapped in different numbers, rewrite it — it's a template, not a summary.
- 1-3 plain sentences. No bullet points, no markdown, no headers, no emoji. Address the reader as "you". If the situation is unremarkable, say less — a short, low-key sentence beats a padded one."""


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
        # max_completion_tokens, not max_tokens — newer models (including
        # gpt-5.6-luna) reject the legacy Chat Completions parameter name
        # outright with a 400. temperature is dropped rather than guessed
        # at a value: reasoning-capable models often restrict or reject it
        # entirely, and this task has no need for sampling variance anyway
        # — consistent, factual paraphrasing is the goal, not creativity.
        "max_completion_tokens": 200,
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
