from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from ohmydata.core import (
    CoverageError,
    IdentityConflictError,
    InstrumentIdentity,
    InstrumentIdentityIndex,
    InstrumentType,
    IssuerIdentity,
    ProviderInstrumentAlias,
)
from ohmydata.core.errors import ResourceLimitError
from ohmydata.providers.sec import SecDataVersionId, SecDataVersionKind

T = datetime(2025, 1, 1, tzinfo=UTC)
D = date(2025, 1, 1)
END = date(2026, 1, 1)


def declarations(kind=InstrumentType.COMMON_SHARE):
    issuer = IssuerIdentity("synthetic:issuer", "evidence:issuer", T)
    security = InstrumentIdentity(
        "synthetic:security", issuer.issuer_id, kind, "evidence:security", T
    )
    alias = ProviderInstrumentAlias(
        "demo", "XYZ", "XNAS", "USD", security.instrument_id, D, END, "evidence:listing", T
    )
    return issuer, security, alias


def resolve(index, **kwargs):
    return index.resolve(
        **{
            "provider": "demo",
            "alias": "XYZ",
            "venue": "XNAS",
            "effective_date": D,
            "knowledge_cutoff": T,
            **kwargs,
        }
    )


def test_ticker_reuse_half_open_boundaries():
    issuer, security, alias = declarations(InstrumentType.ETF)
    company = replace(issuer, issuer_id="synthetic:company")
    share = replace(
        security,
        instrument_id="synthetic:share",
        issuer_id=company.issuer_id,
        instrument_type=InstrumentType.COMMON_SHARE,
    )
    later = replace(
        alias, instrument_id=share.instrument_id, effective_from=END, effective_to=date(2027, 1, 1)
    )
    index = InstrumentIdentityIndex([issuer, company], [security, share], [later, alias])
    assert resolve(index).instrument == security
    assert resolve(index, effective_date=END).instrument == share
    for outside in [D - timedelta(days=1), date(2027, 1, 1)]:
        with pytest.raises(CoverageError):
            resolve(index, effective_date=outside)


def test_classes_adr_venues_and_exact_alias():
    issuer, security, alias = declarations()
    adr = replace(security, instrument_id="synthetic:adr", instrument_type=InstrumentType.ADR)
    other = replace(security, instrument_id="synthetic:class-b")
    index = InstrumentIdentityIndex(
        [issuer],
        [security, adr, other],
        [
            alias,
            replace(alias, instrument_id=adr.instrument_id, venue="XNYS"),
            replace(alias, instrument_id=other.instrument_id, alias="XYZ.B"),
        ],
    )
    assert resolve(index).instrument == security
    assert resolve(index, venue="XNYS").instrument == adr
    assert resolve(index, alias="XYZ.B").instrument == other
    with pytest.raises(CoverageError):
        resolve(index, alias="xyz")


def test_index_nulls():
    _, security, alias = declarations()
    security = replace(security, issuer_id=None, instrument_type=InstrumentType.INDEX)
    result = resolve(InstrumentIdentityIndex([], [security], [replace(alias, currency=None)]))
    assert result.issuer is None and result.alias_declaration.currency is None
    with pytest.raises(ValueError):
        replace(security, instrument_type=InstrumentType.ADR)


@pytest.mark.parametrize("layer", ["issuer", "instrument", "alias"])
def test_future_recorded_evidence(layer):
    issuer, security, alias = declarations()
    future = T + timedelta(days=1)
    if layer == "issuer":
        issuer = replace(issuer, recorded_at=future)
    if layer in {"issuer", "instrument"}:
        security = replace(security, recorded_at=future)
    alias = replace(alias, recorded_at=future)
    index = InstrumentIdentityIndex([issuer], [security], [alias])
    with pytest.raises(CoverageError):
        resolve(index)
    assert resolve(index, knowledge_cutoff=future).declaration_recorded_bound == future


def test_overlap_rejects_proposed_catalog_old_catalog_unchanged():
    issuer, security, alias = declarations()
    old = InstrumentIdentityIndex([issuer], [security], [alias])
    future = replace(alias, recorded_at=T + timedelta(days=1))
    with pytest.raises(IdentityConflictError):
        InstrumentIdentityIndex([issuer], [security], [alias, future])
    assert resolve(old).alias_declaration == alias


def test_conflicts_references_and_causality():
    issuer, security, alias = declarations()
    with pytest.raises(IdentityConflictError):
        InstrumentIdentityIndex(
            [issuer, replace(issuer, evidence_reference="evidence:other")], [], []
        )
    with pytest.raises(IdentityConflictError):
        InstrumentIdentityIndex(
            [issuer], [security, replace(security, instrument_type=InstrumentType.ETF)], []
        )
    for issuers, securities, aliases in [([], [security], []), ([issuer], [], [alias])]:
        with pytest.raises(CoverageError):
            InstrumentIdentityIndex(issuers, securities, aliases)
    for issuers, securities, aliases in [
        ([replace(issuer, recorded_at=T + timedelta(days=1))], [security], []),
        ([issuer], [replace(security, recorded_at=T + timedelta(days=1))], [alias]),
        ([issuer], [security], [replace(alias, currency=None)]),
    ]:
        with pytest.raises(ValueError):
            InstrumentIdentityIndex(issuers, securities, aliases)


def test_dedup_copy_immutable_and_stable_binding():
    issuer, security, alias = declarations()
    index = InstrumentIdentityIndex([issuer, issuer], [security], [alias, alias])
    plain = InstrumentIdentityIndex([issuer], [security], [alias])
    assert index.identity == plain.identity
    result = resolve(index)
    later = resolve(index, knowledge_cutoff=T + timedelta(days=1))
    assert result.binding_identity == later.binding_identity
    assert result.resolution_identity != later.resolution_identity
    larger = InstrumentIdentityIndex([issuer], [security], [replace(alias, alias="OTHER"), alias])
    assert resolve(larger).binding_identity == result.binding_identity
    changed = replace(issuer, evidence_reference="evidence:correction")
    assert (
        resolve(InstrumentIdentityIndex([changed], [security], [alias])).binding_identity
        != result.binding_identity
    )
    assert (
        SecDataVersionId(SecDataVersionKind.INSTRUMENT_IDENTITY, result.binding_identity).identity
        == result.binding_identity
    )
    with pytest.raises(FrozenInstanceError):
        result.instrument.instrument_id = "changed"
    with pytest.raises(TypeError):
        index._issuers["new"] = issuer
    object.__setattr__(issuer, "issuer_id", "tampered")
    assert resolve(index).issuer.issuer_id == "synthetic:issuer"


@pytest.mark.parametrize("cap", [True, 0, 10001, 1.5])
def test_invalid_cap_before_consumption(cap):
    def forbidden():
        raise AssertionError("consumed")
        yield

    with pytest.raises(ValueError):
        InstrumentIdentityIndex(forbidden(), [], [], max_records=cap)


def test_aggregate_budget_before_dedup():
    issuer, security, alias = declarations()
    assert resolve(InstrumentIdentityIndex([issuer], [security], [alias], max_records=3))
    seen = []

    def records():
        for i in range(100):
            seen.append(i)
            yield issuer

    with pytest.raises(ResourceLimitError):
        InstrumentIdentityIndex(records(), [], [], max_records=3)
    assert len(seen) == 4
    with pytest.raises(ResourceLimitError):
        InstrumentIdentityIndex([issuer, issuer], [security], [alias], max_records=3)


@pytest.mark.parametrize(
    "bad", ["", " leading", "trailing ", "a\n", "a\x00b", "x" * 1025, "界" * 342]
)
def test_text_limits(bad):
    with pytest.raises(ValueError):
        IssuerIdentity(bad, "evidence:synthetic", T)


def test_exact_types_and_utc():
    issuer, security, alias = declarations()
    assert IssuerIdentity("x" * 1024, "evidence:synthetic", T)
    assert replace(issuer, recorded_at=T.astimezone(timezone(timedelta(hours=8)))) == issuer
    for changes in [{"effective_from": T}, {"recorded_at": T.replace(tzinfo=None)}]:
        with pytest.raises(TypeError):
            replace(alias, **changes)
    for changes in [{"effective_to": D}, {"currency": "usd"}, {"currency": "美元"}]:
        with pytest.raises(ValueError):
            replace(alias, **changes)
    index = InstrumentIdentityIndex([issuer], [security], [alias])
    with pytest.raises(TypeError):
        resolve(index, effective_date=T)
    with pytest.raises(TypeError):
        resolve(index, knowledge_cutoff=T.replace(tzinfo=None))
