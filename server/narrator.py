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
import asyncio
import hashlib
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
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

# Bump this on any change to _SYSTEM_PROMPT below — it's folded into the
# cache hash, so bumping it is what makes a prompt edit actually reach
# users instead of every previously-cached sentence just sitting there
# looking unaffected until its own facts happen to change.
PROMPT_VERSION = 2

# Hard floor, independent of the hash-based caching above: no (subject,
# section) may call the API more than once in this window, full stop,
# even if its facts genuinely changed twice within it. This exists as a
# circuit breaker for the failure mode this system actually hit — a
# too-frequent page auto-refresh multiplying calls far beyond what the
# facts justified. Caching alone assumes the request rate is sane; this
# doesn't assume that.
MIN_REGEN_SECONDS = 300

# Per-(subject,section) locks, scoped to this process. Two overlapping
# requests for the same row (two tabs, a refresh landing mid-request) used
# to both pass the "no cached row yet" check before either had committed,
# both call the API, and both try to insert — creating duplicate rows that
# then made the cache lookup itself unreliable (get_narrative could hit
# either duplicate depending on the SELECT order, so unrelated calls
# started re-triggering too). This serializes access to a given key
# instead.
_locks: dict[str, asyncio.Lock] = {}

def _lock_for(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]

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
    # PROMPT_VERSION is part of the hash on purpose: the cache is keyed on
    # "what would produce a different answer", and a changed system prompt
    # does exactly that even when the underlying facts haven't moved.
    # Without this, editing the prompt silently kept serving every
    # previously-cached sentence until its own facts happened to change —
    # which is exactly what happened the first time the prompt was tuned.
    payload = json.dumps({"section": section, "facts": facts, "prompt_version": PROMPT_VERSION},
                          sort_keys=True, default=str)
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
        "max_completion_tokens": 400,
        # gpt-5.6-luna defaults to *medium* reasoning effort, which spends
        # part of the token budget on hidden reasoning before it ever
        # writes visible output — with the old 200-token ceiling that ate
        # the entire budget and every single call failed with "max_tokens
        # or model output limit was reached", silently, on every page
        # load, forever (see get_narrative's failure-tracking for why that
        # was also unrate-limited). This task is templated paraphrasing of
        # a facts dict, not analysis — it has no use for reasoning at all,
        # so turn it off outright rather than just budgeting around it.
        "reasoning_effort": "none",
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

    key = f"{subject_type}:{subject_id}:{section}"
    async with _lock_for(key):
        new_hash = _facts_hash(section, facts)
        q = await db.execute(select(Narrative).where(
            Narrative.subject_type == subject_type,
            Narrative.subject_id == subject_id,
            Narrative.section == section,
        ).order_by(Narrative.id.desc()))
        row = q.scalars().first()

        if row and row.source_hash == new_hash:
            return row.narrative_text or None

        # Rate floor keyed on last_attempt_at, NOT generated_at — a section
        # whose every call fails never sets generated_at, so a floor keyed
        # only on that field would never engage for exactly the case that
        # needs it: unbounded retries of a persistently-failing call.
        if row and row.last_attempt_at:
            last = row.last_attempt_at.replace(tzinfo=timezone.utc) if row.last_attempt_at.tzinfo is None else row.last_attempt_at
            age = (datetime.now(timezone.utc) - last).total_seconds()
            if age < MIN_REGEN_SECONDS:
                logger.info(f"[INFO] Skipping regen for {key} - last attempt {int(age)}s ago (floor: {MIN_REGEN_SECONDS}s)")
                return row.narrative_text or None

        # Record the attempt BEFORE calling the API, and commit immediately
        # — so even if this call fails, hangs, or the process dies mid-call,
        # the next request still sees a recent last_attempt_at and backs
        # off, instead of every concurrent/subsequent request racing to
        # retry a call that was already known to be failing.
        #
        # source_hash/narrative_text get "" here, not left unset — the
        # live table predates this column's nullable=True (this app's
        # auto-migration only ever adds new columns, it doesn't relax an
        # existing NOT NULL), so a brand-new row for a first-ever failed
        # attempt still has to satisfy that constraint. "" reads the same
        # as no-narrative-yet everywhere this value is consumed (falsy in
        # both Python and the JS that renders it) without needing a manual
        # ALTER TABLE against a live database.
        if row:
            row.last_attempt_at = datetime.now(timezone.utc)
        else:
            row = Narrative(subject_type=subject_type, subject_id=subject_id, section=section,
                             source_hash="", narrative_text="",
                             last_attempt_at=datetime.now(timezone.utc))
            db.add(row)
        await db.commit()

        text = _call_openai(facts, section)
        if text is None:
            # Generation failed — serve the last good narrative rather than
            # nothing, if one exists (None if there's never been one). The
            # attempt is already recorded above, so this failure is rate-
            # limited on the next call regardless of what happens here.
            return row.narrative_text or None

        row.source_hash = new_hash
        row.narrative_text = text
        row.generated_at = datetime.now(timezone.utc)
        await db.commit()
        return text
