"""Funding-round bookkeeping: deduplicate announcements, corroborate, and flag conflicts."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import RAISED_STATUSES, FundingRound, Investor, NewsArticle, RoundInvestor, SourceObservation, Startup
from .text import normalize_company_name

SAME_EVENT_WINDOW_DAYS = 60
AMOUNT_TOLERANCE = 0.10  # amounts within 10% are treated as the same figure (rounding in headlines)

STATUS_RANK = {"regulatory_filing": 0, "confirmed": 1, "company_announced": 2, "reported": 3, "analyst_entered": 4,
               "target": 5, "rumor": 6}


def _amounts_agree(a: float | None, b: float | None) -> bool | None:
    if a is None or b is None:
        return None
    hi = max(a, b)
    return hi == 0 or abs(a - b) / hi <= AMOUNT_TOLERANCE


def get_or_create_investor(session: Session, name: str) -> Investor:
    norm = normalize_company_name(name)
    inv = session.scalar(select(Investor).where(Investor.normalized_name == norm))
    if not inv:
        inv = Investor(name=name, normalized_name=norm)
        session.add(inv)
        session.flush()
    return inv


def attach_investors(session: Session, rnd: FundingRound, investors: list[str], leads: list[str], source_url: str | None) -> None:
    existing = {ri.investor_id for ri in rnd.investors}
    for name in investors:
        inv = get_or_create_investor(session, name)
        if inv.id in existing:
            continue
        rnd.investors.append(RoundInvestor(investor_id=inv.id, is_lead=name in leads, source_url=source_url))
        existing.add(inv.id)


def _publisher_key(value: str | None) -> str:
    """Normalize publisher labels so casing/spacing differences do not fake independence."""
    return " ".join((value or "").casefold().split())


def _append_note(rnd: FundingRound, note: str) -> None:
    if not note:
        return
    existing = [part.strip() for part in (rnd.notes or "").split(" | ") if part.strip()]
    if note not in existing:
        existing.append(note)
        rnd.notes = " | ".join(existing)


def _set_resolution(rnd: FundingRound, state: str, reason: str, candidate_ids: list[int] | None = None) -> None:
    extra = dict(rnd.extra or {})
    extra["funding_resolution"] = {
        "state": state, "reason": reason,
        "candidate_round_ids": sorted(set(candidate_ids or [])), "rule_version": 1,
    }
    rnd.extra = extra


def _observation_is_current(observation: dict) -> bool:
    # Rows written before the currentness flag was added are treated as current until superseded.
    return observation.get("is_current", True) is not False


def _current_observations(rnd: FundingRound) -> list[dict]:
    return [o for o in ((rnd.extra or {}).get("funding_observations") or []) if _observation_is_current(o)]


def _known_independent_origins(rnd: FundingRound, *, include_history: bool = False) -> set[str]:
    observations = list((rnd.extra or {}).get("funding_observations") or [])
    if not include_history:
        observations = [o for o in observations if _observation_is_current(o)]
    return {
        o["origin_group"] for o in observations
        if o.get("source_role") == "independent_reporter" and o.get("origin_group")
        and o.get("evidence_status") in RAISED_STATUSES
    }


def _is_independent_source(
    rnd: FundingRound, *, origin_group: str | None, source_role: str | None,
    syndicated_copy: bool, source_revision: bool,
) -> bool:
    """Only a known independent-reporting origin can corroborate; revisions never count twice."""
    if source_revision or syndicated_copy or source_role != "independent_reporter" or not origin_group:
        return False
    return origin_group not in _known_independent_origins(rnd, include_history=True)


def _refresh_evidence_status(rnd: FundingRound) -> None:
    """Derive the summary status from current claim observations, not publisher-string counts."""
    observations = _current_observations(rnd)
    statuses = [o.get("evidence_status") for o in observations if o.get("evidence_status")]
    if not statuses:
        return

    reporter_origins = {
        o.get("origin_group") for o in observations
        if o.get("source_role") == "independent_reporter" and o.get("origin_group")
        and o.get("evidence_status") in RAISED_STATUSES
    }
    company_statement = any(
        status == "company_announced" for status in statuses
    ) or any(
        o.get("source_role") in {"company_statement", "press_release_wire"}
        and o.get("evidence_status") in RAISED_STATUSES
        for o in observations
    )

    # "Confirmed" requires 2 distinct known independent reporter origins, or a source
    # statement plus a distinct independent reporter origin. Publisher names alone do not count.
    if len(reporter_origins) >= 2 or (company_statement and len(reporter_origins) >= 1):
        rnd.evidence_status = "confirmed"
    elif company_statement:
        rnd.evidence_status = "company_announced"
    elif any(status in {"reported", "confirmed"} for status in statuses) or reporter_origins:
        rnd.evidence_status = "reported"
    elif "analyst_entered" in statuses:
        rnd.evidence_status = "analyst_entered"
    else:
        candidates = [status for status in statuses if status in STATUS_RANK]
        if candidates:
            rnd.evidence_status = min(candidates, key=lambda status: STATUS_RANK[status])


def _record_observation(
    rnd: FundingRound, *, round_type: str | None, amount: float | None, currency: str | None,
    amount_text: str | None, announced: date | None, announced_date_basis: str | None,
    source_published_at: str | None, evidence_status: str, publisher: str | None,
    source_url: str | None, article_id: int | None, sec_filing_id: int | None,
    outcome: str, reason: str, independent_source: bool,
    source_observation_id: int | None = None, origin_group: str | None = None,
    source_role: str | None = None,
) -> None:
    """Keep each source version separately; mark prior versions of the same source inactive."""
    extra = dict(rnd.extra or {})
    extra.setdefault("announced_date_basis", announced_date_basis or ("unknown" if announced is None else "unspecified"))
    observations = list(extra.get("funding_observations") or [])

    for observation in observations:
        if source_observation_id is not None and observation.get("source_observation_id") == source_observation_id:
            return
        if (source_observation_id is None and article_id is not None
                and observation.get("article_id") == article_id
                and observation.get("source_observation_id") is None):
            return
        if (source_observation_id is None and article_id is None and sec_filing_id is not None
                and observation.get("sec_filing_id") == sec_filing_id):
            return
        if (source_observation_id is None and article_id is None and sec_filing_id is None
                and source_url and observation.get("source_url") == source_url):
            return
        if (article_id is not None and observation.get("article_id") == article_id
                and source_observation_id is not None
                and observation.get("source_observation_id") != source_observation_id
                and _observation_is_current(observation)):
            observation["is_current"] = False
            observation["superseded_by_source_observation_id"] = source_observation_id

    observations.append({
        "article_id": article_id, "source_observation_id": source_observation_id,
        "sec_filing_id": sec_filing_id, "source_url": source_url,
        "publisher": publisher, "origin_group": origin_group, "source_role": source_role,
        "round_type": round_type, "amount": amount, "currency": currency, "amount_text": amount_text,
        "announced_date": announced.isoformat() if announced else None,
        "announced_date_basis": announced_date_basis or ("unknown" if announced is None else "unspecified"),
        "source_published_at": source_published_at, "evidence_status": evidence_status,
        "outcome": outcome, "match_reason": reason, "independent_source": bool(independent_source),
        "is_current": True,
    })
    extra["funding_observations"] = observations
    rnd.extra = extra


def supersede_source_revision(session: Session, article_id: int, current_source_observation_id: int) -> None:
    """Deactivate funding claims from an earlier revision before the new version is extracted.

    If a legacy round has publisher attribution but no per-source observations, preserve the
    unattributed support explicitly rather than silently treating it as disproved.
    """
    source_versions = list(session.scalars(
        select(SourceObservation).where(SourceObservation.article_id == article_id)
        .order_by(SourceObservation.revision_number.desc())
    ))
    previous_source = next((o for o in source_versions if o.id != current_source_observation_id), None)
    startup_ids: set[int] = set()

    for rnd in session.scalars(select(FundingRound)):
        extra = dict(rnd.extra or {})
        observations = list(extra.get("funding_observations") or [])
        linked = [o for o in observations if o.get("article_id") == article_id]
        if not linked and rnd.article_id == article_id:
            previous_publisher = previous_source.publisher if previous_source else (
                (rnd.publishers or [None])[0] if rnd.publishers else None
            )
            old_status = "company_announced" if previous_source and previous_source.source_type == "press_release" else "reported"
            baseline = {
                "article_id": article_id,
                "source_observation_id": previous_source.id if previous_source else None,
                "sec_filing_id": rnd.sec_filing_id, "source_url": previous_source.source_url if previous_source else rnd.source_url,
                "publisher": previous_publisher,
                "origin_group": previous_source.origin_group if previous_source else None,
                "source_role": previous_source.source_role if previous_source else None,
                "round_type": rnd.round_type, "amount": rnd.amount, "currency": rnd.currency,
                "amount_text": rnd.amount_text,
                "announced_date": rnd.announced_date.isoformat() if rnd.announced_date else None,
                "announced_date_basis": (extra.get("announced_date_basis") or "unknown"),
                "source_published_at": previous_source.published_at.isoformat() if previous_source and previous_source.published_at else None,
                "evidence_status": old_status, "outcome": "legacy_baseline",
                "match_reason": "Backfilled from the pre-revision funding-round value",
                "independent_source": False, "is_current": True,
            }
            observations.append(baseline)
            linked = [baseline]

        if not linked:
            continue
        changed = False
        for observation in linked:
            if observation.get("source_observation_id") == current_source_observation_id:
                continue
            if _observation_is_current(observation):
                if observation.get("source_observation_id") is None and previous_source:
                    observation["source_observation_id"] = previous_source.id
                observation["is_current"] = False
                observation["superseded_by_source_observation_id"] = current_source_observation_id
                changed = True
        if not changed:
            continue

        # Legacy rows can have publisher names but no source-linked observations. Represent
        # those remaining names as unknown-origin support instead of deleting their claim.
        if not extra.get("funding_observations") and len(rnd.publishers or []) > 1:
            for publisher in (rnd.publishers or [])[1:]:
                observations.append({
                    "article_id": None, "source_observation_id": None, "sec_filing_id": None,
                    "source_url": None, "publisher": publisher, "origin_group": None, "source_role": None,
                    "round_type": rnd.round_type, "amount": rnd.amount, "currency": rnd.currency,
                    "amount_text": rnd.amount_text,
                    "announced_date": rnd.announced_date.isoformat() if rnd.announced_date else None,
                    "announced_date_basis": extra.get("announced_date_basis") or "unknown",
                    "source_published_at": None, "evidence_status": rnd.evidence_status,
                    "outcome": "legacy_unattributed", "match_reason": "Legacy publisher attribution has no claim-level link",
                    "independent_source": False, "is_current": True,
                })

        extra["funding_observations"] = observations
        rnd.extra = extra
        if not any(_observation_is_current(o) for o in observations):
            rnd.is_current = False
        else:
            rnd.is_current = True
            _refresh_evidence_status(rnd)
        startup_ids.add(rnd.startup_id)

    for startup_id in startup_ids:
        startup = session.get(Startup, startup_id)
        if startup:
            refresh_funding_summary(startup)




def record_round(
    session: Session, startup: Startup, *, round_type: str | None, amount: float | None,
    currency: str | None, amount_text: str | None, announced: date | None, evidence_status: str,
    publisher: str | None, source_url: str | None, article_id: int | None = None,
    sec_filing_id: int | None = None, investors: list[str] | None = None, leads: list[str] | None = None,
    announced_date_basis: str | None = None, source_published_at: str | None = None,
    source_observation_id: int | None = None, origin_group: str | None = None,
    source_role: str | None = None,
) -> tuple[FundingRound, str]:
    """Record a source-backed funding observation without inflating corroboration or losing revisions.

    Returns (round, outcome): 'new' | 'revised' | 'corroborated' | 'same_source' | 'conflict'.
    Underdetermined observations remain separate and are flagged for review.
    """
    incoming_article = session.get(NewsArticle, article_id) if article_id is not None else None
    if source_observation_id is None and article_id is not None:
        latest_source = session.scalar(select(SourceObservation).where(
            SourceObservation.article_id == article_id
        ).order_by(SourceObservation.revision_number.desc()).limit(1))
        if latest_source:
            source_observation_id = latest_source.id
    source_observation = session.get(SourceObservation, source_observation_id) if source_observation_id is not None else None
    if source_observation:
        origin_group = origin_group or source_observation.origin_group
        source_role = source_role or source_observation.source_role
    syndicated_copy = bool(incoming_article and incoming_article.duplicate_of_id is not None)

    # A changed observation from an already-seen URL is a revision, not a repeat of the same source.
    revision_rows: list[FundingRound] = []
    for existing in startup.rounds:
        observations = (existing.extra or {}).get("funding_observations") or []
        if source_observation_id is not None and any(
            o.get("source_observation_id") == source_observation_id for o in observations
        ):
            return existing, "same_source"
        linked_to_article = article_id is not None and (
            existing.article_id == article_id or any(o.get("article_id") == article_id for o in observations)
        )
        if linked_to_article and source_observation_id is not None:
            revision_rows.append(existing)
            continue
        if linked_to_article and source_observation_id is None:
            return existing, "same_source"
        if source_observation_id is None and source_url and (
            existing.source_url == source_url or any(o.get("source_url") == source_url for o in observations)
        ):
            return existing, "same_source"

    revision_row = next((r for r in revision_rows if not r.is_current), revision_rows[0] if revision_rows else None)
    is_source_revision = bool(revision_rows)

    compatible = [
        r for r in startup.rounds
        if r.is_current
        and r.evidence_status != "regulatory_filing" and evidence_status != "regulatory_filing"
        and (r.round_type is None or round_type is None or r.round_type == round_type)
    ]
    if announced is not None:
        window_lo = announced - timedelta(days=SAME_EVENT_WINDOW_DAYS)
        window_hi = announced + timedelta(days=SAME_EVENT_WINDOW_DAYS)
        dated_candidates = [
            r for r in compatible if r.announced_date is not None
            and window_lo <= r.announced_date <= window_hi
        ]
    else:
        window_lo, window_hi = None, None
        dated_candidates = []

    def fully_comparable(rnd: FundingRound) -> bool:
        # Match amounts only when date, round type and currency are explicitly comparable.
        return (
            announced is not None and rnd.announced_date is not None
            and (rnd.round_type is None or round_type is None or rnd.round_type == round_type)
            and rnd.currency is not None and currency is not None and rnd.currency == currency
            and rnd.evidence_status != "regulatory_filing" and evidence_status != "regulatory_filing"
        )

    conflicts = [
        r for r in dated_candidates
        if fully_comparable(r) and r.round_type is not None and round_type is not None
        and r.round_type == round_type and _amounts_agree(r.amount, amount) is False
        and r.evidence_status in RAISED_STATUSES and evidence_status in RAISED_STATUSES
    ]
    strong_matches = [
        r for r in dated_candidates
        if fully_comparable(r) and _amounts_agree(r.amount, amount) is True
    ]

    # Unknown dates/amounts/currency and even a conflicting round label within the same
    # time window are reviewable ambiguities, not silent distinct events.
    uncertain_candidates = []
    for candidate in compatible:
        if announced is not None and candidate.announced_date is not None:
            if not window_lo <= candidate.announced_date <= window_hi:
                continue
            date_unknown = False
        else:
            date_unknown = True
        fields_missing = (
            candidate.currency is None or currency is None
            or candidate.amount is None or amount is None
            or (candidate.currency is not None and currency is not None and candidate.currency != currency)
        )
        if not date_unknown and not fields_missing:
            continue
        amount_consistent = True
        if (candidate.amount is not None and amount is not None
                and candidate.currency is not None and currency is not None
                and candidate.currency == currency):
            amount_consistent = _amounts_agree(candidate.amount, amount) is not False
        if amount_consistent:
            uncertain_candidates.append(candidate)

    for candidate in startup.rounds:
        if not candidate.is_current or candidate.evidence_status == "regulatory_filing":
            continue
        if not candidate.round_type or not round_type or candidate.round_type == round_type:
            continue
        same_window = (
            announced is None or candidate.announced_date is None
            or (window_lo is not None and window_lo <= candidate.announced_date <= window_hi)
        )
        if same_window and candidate not in uncertain_candidates:
            uncertain_candidates.append(candidate)

    plausible = list(strong_matches)
    seen_ids = {r.id for r in plausible}
    for candidate in uncertain_candidates:
        if candidate.id not in seen_ids:
            plausible.append(candidate)
            seen_ids.add(candidate.id)
    ambiguous = len(strong_matches) > 1 or bool(uncertain_candidates) or (
        announced is None and bool(plausible)
    )

    def supersede_revision_row(target: FundingRound) -> None:
        if revision_row and revision_row is not target and not revision_row.is_current:
            revision_row.superseded_by_round_id = target.id
            _set_resolution(revision_row, "superseded", f"Source revision attached to funding round {target.id}", [target.id])

    if conflicts:
        candidate_ids = [r.id for r in conflicts if r.id is not None]
        rnd = FundingRound(
            startup_id=startup.id, round_type=round_type, amount=amount, currency=currency, amount_text=amount_text,
            announced_date=announced, evidence_status=evidence_status, source_url=source_url, article_id=article_id,
            sec_filing_id=sec_filing_id, publishers=[publisher] if publisher and not syndicated_copy else [],
            conflict=True,
        )
        startup.rounds.append(rnd)
        session.flush()
        note = (
            f"Conflicting amounts for the same {round_type or 'round'} within {SAME_EVENT_WINDOW_DAYS} days: "
            f"{amount_text or amount} ({publisher or 'source'}) conflicts with round(s) "
            f"{', '.join(str(i) for i in candidate_ids)}"
        )
        rnd.conflict_note = note
        _set_resolution(rnd, "conflict", note, candidate_ids)
        for other in conflicts:
            other.conflict, other.conflict_note = True, note
            _set_resolution(other, "conflict", note, [rnd.id])
        startup.add_flag("conflicting_funding")
        _record_observation(
            rnd, round_type=round_type, amount=amount, currency=currency, amount_text=amount_text,
            announced=announced, announced_date_basis=announced_date_basis,
            source_published_at=source_published_at, evidence_status=evidence_status, publisher=publisher,
            source_url=source_url, article_id=article_id, sec_filing_id=sec_filing_id,
            source_observation_id=source_observation_id, origin_group=origin_group, source_role=source_role,
            outcome="conflict", reason=note,
            independent_source=_is_independent_source(rnd, origin_group=origin_group, source_role=source_role,
                                                      syndicated_copy=syndicated_copy, source_revision=is_source_revision),
        )
        _refresh_evidence_status(rnd)
        for other in conflicts:
            _refresh_evidence_status(other)
        supersede_revision_row(rnd)
        attach_investors(session, rnd, investors or [], leads or [], source_url)
        return rnd, "conflict"

    if len(strong_matches) == 1 and not ambiguous:
        rnd = strong_matches[0]
        publishers = list(rnd.publishers or [])
        existing_keys = {_publisher_key(p) for p in publishers if _publisher_key(p)}
        incoming_key = _publisher_key(publisher)
        independent_source = _is_independent_source(
            rnd, origin_group=origin_group, source_role=source_role,
            syndicated_copy=syndicated_copy, source_revision=is_source_revision,
        )

        unconfirmed = {"rumor", "target"}
        if rnd.evidence_status in unconfirmed and evidence_status in RAISED_STATUSES:
            earlier = ", ".join(publishers) or "unknown"
            _append_note(rnd, f"Earlier {rnd.evidence_status} from {earlier}")
            publishers = []
            existing_keys = set()
        elif evidence_status in unconfirmed and rnd.evidence_status in RAISED_STATUSES:
            reason = "Rumour/target retained as source evidence but not used to confirm an already reported round"
            _set_resolution(rnd, "matched", reason, [rnd.id])
            _record_observation(
                rnd, round_type=round_type, amount=amount, currency=currency, amount_text=amount_text,
                announced=announced, announced_date_basis=announced_date_basis,
                source_published_at=source_published_at, evidence_status=evidence_status, publisher=publisher,
                source_url=source_url, article_id=article_id, sec_filing_id=sec_filing_id,
                source_observation_id=source_observation_id, origin_group=origin_group, source_role=source_role,
                outcome="same_source", reason=reason, independent_source=False,
            )
            _refresh_evidence_status(rnd)
            supersede_revision_row(rnd)
            return rnd, "same_source"

        if incoming_key and incoming_key not in existing_keys and not syndicated_copy:
            publishers.append(publisher)
        rnd.publishers = publishers
        outcome = "corroborated" if independent_source else "same_source"
        reason = (
            f"Unique candidate with no known round-type contradiction and matching currency; event dates are within "
            f"{SAME_EVENT_WINDOW_DAYS} days and amounts agree within {AMOUNT_TOLERANCE:.0%}"
        )
        if syndicated_copy:
            reason += "; syndicated copy excluded from independent corroboration"
        _set_resolution(rnd, "matched", reason, [rnd.id])
        _record_observation(
            rnd, round_type=round_type, amount=amount, currency=currency, amount_text=amount_text,
            announced=announced, announced_date_basis=announced_date_basis,
            source_published_at=source_published_at, evidence_status=evidence_status, publisher=publisher,
            source_url=source_url, article_id=article_id, sec_filing_id=sec_filing_id,
            source_observation_id=source_observation_id, origin_group=origin_group, source_role=source_role,
            outcome=outcome, reason=reason, independent_source=independent_source,
        )
        _refresh_evidence_status(rnd)
        supersede_revision_row(rnd)
        attach_investors(session, rnd, investors or [], leads or [], source_url)
        return rnd, outcome

    # If a source had only one active assertion and no replacement candidate exists, revise
    # the same canonical row in place. This preserves its history without counting two rounds.
    if (revision_row and not revision_row.is_current and not conflicts and not strong_matches
            and not uncertain_candidates and not ambiguous):
        rnd = revision_row
        rnd.round_type, rnd.amount, rnd.currency, rnd.amount_text = round_type, amount, currency, amount_text
        rnd.announced_date, rnd.evidence_status = announced, evidence_status
        rnd.source_url, rnd.article_id, rnd.sec_filing_id = source_url, article_id, sec_filing_id
        rnd.publishers = [publisher] if publisher and not syndicated_copy else []
        rnd.is_current = True
        rnd.superseded_by_round_id = None
        rnd.conflict = False
        rnd.conflict_note = None
        reason = "Revised source observation replaced the prior value; earlier observation remains in history"
        _set_resolution(rnd, "revised", reason, [rnd.id])
        _record_observation(
            rnd, round_type=round_type, amount=amount, currency=currency, amount_text=amount_text,
            announced=announced, announced_date_basis=announced_date_basis,
            source_published_at=source_published_at, evidence_status=evidence_status, publisher=publisher,
            source_url=source_url, article_id=article_id, sec_filing_id=sec_filing_id,
            source_observation_id=source_observation_id, origin_group=origin_group, source_role=source_role,
            outcome="revised", reason=reason, independent_source=False,
        )
        _refresh_evidence_status(rnd)
        if not any(r.is_current and r.conflict for r in startup.rounds):
            startup.remove_flag("conflicting_funding")
        refresh_funding_summary(startup)
        attach_investors(session, rnd, investors or [], leads or [], source_url)
        return rnd, "revised"

    rnd = FundingRound(
        startup_id=startup.id, round_type=round_type, amount=amount, currency=currency, amount_text=amount_text,
        announced_date=announced, evidence_status=evidence_status, source_url=source_url, article_id=article_id,
        sec_filing_id=sec_filing_id, publishers=[publisher] if publisher and not syndicated_copy else [],
    )
    startup.rounds.append(rnd)
    session.flush()
    attach_investors(session, rnd, investors or [], leads or [], source_url)

    if ambiguous:
        candidate_ids = [r.id for r in plausible if r.id is not None]
        reason = (
            "Potential duplicate not auto-merged because the event date, currency, round type, or amount "
            "is missing/incompatible, or more than one candidate fits; review linked source observations"
        )
        _append_note(rnd, reason)
        for other in plausible:
            _append_note(other, f"Possible related funding observation in round {rnd.id}; review before merging")
            _set_resolution(other, "needs_review", reason, [rnd.id])
        startup.add_flag("possible_duplicate")
        _set_resolution(rnd, "needs_review", reason, candidate_ids)
    else:
        reason = "No unique candidate met the required date, round-type, currency and amount match rules"
        _set_resolution(rnd, "distinct", reason)

    _record_observation(
        rnd, round_type=round_type, amount=amount, currency=currency, amount_text=amount_text,
        announced=announced, announced_date_basis=announced_date_basis,
        source_published_at=source_published_at, evidence_status=evidence_status, publisher=publisher,
        source_url=source_url, article_id=article_id, sec_filing_id=sec_filing_id,
        source_observation_id=source_observation_id, origin_group=origin_group, source_role=source_role,
        outcome="new", reason=reason,
        independent_source=_is_independent_source(rnd, origin_group=origin_group, source_role=source_role,
                                                  syndicated_copy=syndicated_copy, source_revision=is_source_revision),
    )
    _refresh_evidence_status(rnd)
    supersede_revision_row(rnd)
    return rnd, "new"




def _combine_status(current: str, new: str, independent_publishers: int) -> str:
    """Rumours and targets never become 'confirmed' by repetition alone."""
    raised = {current, new} & {"company_announced", "reported", "confirmed"}
    if raised:
        if "confirmed" in raised or independent_publishers >= 2:
            return "confirmed"
        return min(raised, key=lambda s: STATUS_RANK[s])
    return min((current, new), key=lambda s: STATUS_RANK.get(s, 99))


def refresh_funding_summary(startup: Startup) -> None:
    """Recompute total / latest funding from rounds. Rumours, targets and Form D offers are excluded
    from the total; Form D is shown separately on the filing itself."""
    active_rounds = [r for r in startup.rounds if r.is_current]
    raised = [r for r in active_rounds if r.evidence_status in RAISED_STATUSES and r.amount is not None]
    unresolved_conflicts = [r for r in active_rounds if r.conflict]
    currencies = {r.currency for r in raised}
    if unresolved_conflicts:
        startup.total_funding_amount, startup.total_funding_currency = None, None
        startup.total_funding_basis = "Unresolved funding conflict; see individual rounds and source observations."
    elif raised and len(currencies) == 1:
        startup.total_funding_amount = sum(r.amount for r in raised)
        startup.total_funding_currency = currencies.pop()
        n_conf = sum(1 for r in raised if r.evidence_status == "confirmed")
        startup.total_funding_basis = (
            f"Sum of {len(raised)} publicly reported round(s) ({n_conf} corroborated). Excludes rumours, targets and "
            "Form D offering amounts. Not audited."
        )
    elif raised:
        startup.total_funding_amount, startup.total_funding_currency = None, None
        startup.total_funding_basis = "Rounds reported in different currencies; see individual rounds (no conversion applied)."
    else:
        startup.total_funding_amount = startup.total_funding_currency = None
        startup.total_funding_basis = "Not disclosed"

    dated = sorted(
        (r for r in active_rounds if r.evidence_status in RAISED_STATUSES | {"regulatory_filing"}),
        key=lambda r: (r.announced_date or date.min, -STATUS_RANK.get(r.evidence_status, 9)),
    )
    if dated:
        latest = dated[-1]
        startup.latest_funding_amount = latest.amount
        startup.latest_funding_currency = latest.currency
        startup.latest_funding_date = latest.announced_date
        startup.latest_funding_status = latest.evidence_status
        if latest.round_type and latest.evidence_status != "regulatory_filing":
            startup.funding_stage = latest.round_type
    if not any(r.is_current and r.conflict for r in startup.rounds):
        startup.remove_flag("conflicting_funding")
