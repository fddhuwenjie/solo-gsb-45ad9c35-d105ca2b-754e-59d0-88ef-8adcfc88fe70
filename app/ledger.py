"""Shared immutable accounting snapshots.

Every endpoint that reads persisted assessment/rules state -- confirming
an assessment, comparing two assessments, planning a remediation and the
pre-publication impact review -- loads the *same* verified projection:
the immutable accounting snapshot.  A snapshot is assembled inside a
single read-only transaction that validates the correspondence between

* the normalized request,
* the persisted result,
* the stored ``summary_hash``,
* the pinned rules snapshot, and
* the immutable revision the assessment recorded.

A legacy row (no pinned rules snapshot) may continue only when its rules
can be restored *and* re-running the assessment with them reproduces the
stored ``summary_hash`` exactly; anything else is refused as a whole
under the calling endpoint's established 404/409 semantics.  Because
every consumer derives its ordering and hash projections from this one
snapshot, no endpoint can silently fall back to the active rules while
another uses the historical ones.
"""

from .common import ConflictError, stable_hash
from .solver import assess_normalized


class SnapshotUnverifiable(ConflictError):
    """HTTP 409: the persisted assessment/rules correspondence is broken.

    Carries the assessment id, its ruleset name (``None`` for inline-rule
    assessments) and whether the row was a pre-snapshot *legacy* row, so
    each endpoint can phrase the refusal in its own established wording
    while sharing this one verification path.
    """

    def __init__(self, aid, ruleset, detail, legacy=False):
        super().__init__(
            "accounting snapshot of assessment %r is not verifiable: %s"
            % (aid, detail),
            {"reason": "rules-snapshot-unverifiable"},
        )
        self.assessment_id = aid
        self.ruleset = ruleset
        self.detail = detail
        self.legacy = legacy


class AccountingSnapshot:
    """The verified, immutable projection of one persisted assessment.

    ``request`` is the persisted normalized request, ``result`` the
    persisted result, ``rules`` the pinned rules snapshot, ``revision``
    the immutable ruleset revision the assessment recorded (``None`` for
    inline-rule assessments) and ``summary_hash`` the stored result hash
    that re-evaluation just reproduced.  ``recovered`` marks a legacy row
    whose rules were restored (and proven) during this load.
    """

    __slots__ = (
        "id",
        "ruleset",
        "revision",
        "request",
        "result",
        "rules",
        "summary_hash",
        "recovered",
    )

    def __init__(self, aid, ruleset, revision, request, result, rules,
                 summary_hash, recovered=False):
        self.id = aid
        self.ruleset = ruleset
        self.revision = revision
        self.request = request
        self.result = result
        self.rules = rules
        self.summary_hash = summary_hash
        self.recovered = recovered

    @property
    def status(self):
        return self.result.get("status")

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<AccountingSnapshot %s ruleset=%r revision=%r>" % (
            self.id,
            self.ruleset,
            self.revision,
        )


def verify_snapshot(aid, ruleset, revision, request, result, rules,
                    summary_hash, revision_content_hash, recovered=False):
    """Validate the full correspondence of one assembled snapshot.

    Pure function: the store fetches every input inside its single read
    transaction (the assessment row, the referenced revision's
    ``content_hash`` and, for legacy rows, the restorable rules), and
    this function checks them, yielding an :class:`AccountingSnapshot`
    that every endpoint may safely sort, project and hash.  Any broken
    correspondence raises :class:`SnapshotUnverifiable`.
    """
    # The id must bind exactly this (normalized request, rules) pair --
    # it is the hash the assessment was persisted under.
    if stable_hash({"request": request, "rules": rules})[:32] != aid:
        raise SnapshotUnverifiable(
            aid,
            ruleset,
            "the stored id does not bind the persisted request and rules "
            "snapshot",
            legacy=recovered,
        )
    # The denormalized summary_hash column must agree with the result.
    if not isinstance(result, dict) or result.get("summary_hash") != summary_hash:
        raise SnapshotUnverifiable(
            aid,
            ruleset,
            "the persisted result does not carry the stored summary_hash",
            legacy=recovered,
        )
    # Re-running the assessment on its own snapshot must reproduce the
    # stored summary_hash -- the result is provably the product of this
    # request and these rules, never of a later ruleset state.
    replayed = assess_normalized(request, rules)
    if replayed.get("summary_hash") != summary_hash:
        raise SnapshotUnverifiable(
            aid,
            ruleset,
            "re-evaluation does not reproduce the persisted summary_hash",
            legacy=recovered,
        )
    # The revision provenance must correspond to the rules snapshot.
    if ruleset is None:
        if revision is not None:
            raise SnapshotUnverifiable(
                aid,
                ruleset,
                "an inline-rule assessment cannot record a ruleset revision",
                legacy=recovered,
            )
    else:
        if revision_content_hash is None:
            raise SnapshotUnverifiable(
                aid,
                ruleset,
                "the recorded revision %r of ruleset %r does not exist"
                % (revision, ruleset),
                legacy=recovered,
            )
        if revision_content_hash != stable_hash(rules):
            raise SnapshotUnverifiable(
                aid,
                ruleset,
                "the pinned rules snapshot does not match revision %r of "
                "ruleset %r" % (revision, ruleset),
                legacy=recovered,
            )
    return AccountingSnapshot(
        aid, ruleset, revision, request, result, rules, summary_hash,
        recovered=recovered,
    )
