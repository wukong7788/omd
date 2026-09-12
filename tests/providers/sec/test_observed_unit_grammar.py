"""Raw unit grammar and namespace boundaries independent of statement selection."""

import pytest

from ohmydata.providers.sec._observed_xbrl_units import decode_raw_units

X = "http://www.xbrl.org/2003/instance"
I = "http://www.xbrl.org/2003/iso4217"


def decode(body, **kwargs):
    return decode_raw_units(
        f'<x:xbrl xmlns:x="{X}" xmlns:i="{I}">{body}</x:xbrl>'.encode(),
        max_elements=kwargs.get("elements", 200),
        max_depth=kwargs.get("depth", 20),
    )


@pytest.mark.parametrize("name", ["-USD", ".USD", "1USD", "USD BAD", "a:b", "", "\u0300USD"])
def test_invalid_qname_local_rejected(name):
    with pytest.raises(ValueError):
        decode(f'<x:unit id="u"><x:measure>i:{name}</x:measure></x:unit>')


@pytest.mark.parametrize(
    "name", ["USD", "_USD", "é", "货币", "A·B", "A\u0300", "\u200cA", "A\u203f", "\U00010000"]
)
def test_xml_unicode_names_preserved(name):
    assert decode(f'<x:unit id="u"><x:measure>i:{name}</x:measure></x:unit>') == {
        "u": f"iso4217:{name}"
    }


@pytest.mark.parametrize(
    "measure,expected",
    [
        ("<x:measure>shares</x:measure>", "{}shares"),
        (f'<x:measure xmlns="{X}">shares</x:measure>', "shares"),
        (f'<x:measure xmlns="{I}">USD</x:measure>', "iso4217:USD"),
        ('<x:measure xmlns="">shares</x:measure>', "{}shares"),
        ("<x:measure>xml:lang</x:measure>", "{http://www.w3.org/XML/1998/namespace}lang"),
        ('<x:measure xmlns:i="urn:different">i:USD</x:measure>', "{urn:different}USD"),
    ],
)
def test_measure_own_namespace_scope(measure, expected):
    assert decode(f'<x:unit id="u">{measure}</x:unit>') == {"u": expected}


def test_aliases_rebinding_and_multiplicity():
    body = f'<x:unit id="a" xmlns:q="{I}"><x:measure>q:USD</x:measure></x:unit><x:unit id="b"><x:measure>i:USD</x:measure></x:unit><x:unit id="c"><x:measure>i:USD</x:measure><x:measure>i:USD</x:measure></x:unit>'
    assert decode(body) == {
        "a": "iso4217:USD",
        "b": "iso4217:USD",
        "c": '{"measures":["iso4217:USD","iso4217:USD"],"type":"product"}',
    }


@pytest.mark.parametrize(
    "body",
    [
        '<x:unit id="u">junk<x:measure>i:USD</x:measure></x:unit>',
        '<x:unit id="u"><x:measure>i:USD</x:measure>junk</x:unit>',
        '<x:unit id="u"><x:measure>i:USD<x:divide/></x:measure></x:unit>',
        '<x:unit id="u"><x:unit id="nested"><x:measure>i:USD</x:measure></x:unit></x:unit>',
        '<x:unit id="u"><x:measure>missing:USD</x:measure></x:unit>',
        '<x:unit id=" "><x:measure>i:USD</x:measure></x:unit>',
        '<x:unit id="u"><x:divide><x:unitNumerator>junk<x:measure>i:USD</x:measure></x:unitNumerator><x:unitDenominator><x:measure>x:shares</x:measure></x:unitDenominator></x:divide></x:unit>',
        '<x:unit id="u"><x:divide><x:unitNumerator><x:measure>i:USD</x:measure></x:unitNumerator><x:unitDenominator>junk<x:measure>x:shares</x:measure></x:unitDenominator></x:divide></x:unit>',
        '<x:unit id="u"><x:divide><x:unitNumerator><x:measure>i:USD</x:measure></x:unitNumerator><x:unitNumerator><x:measure>i:USD</x:measure></x:unitNumerator></x:divide></x:unit>',
    ],
)
def test_malformed_units_fail(body):
    with pytest.raises(ValueError):
        decode(body)


def test_depth_elements_and_byte_budgets():
    body = '<x:unit id="u"><x:measure>i:USD</x:measure></x:unit>'
    assert decode(body, elements=3, depth=3) == {"u": "iso4217:USD"}
    for kwargs in ({"elements": 2}, {"depth": 2}, {"depth": 0}, {"elements": True}):
        with pytest.raises(ValueError):
            decode(body, **kwargs)
    with pytest.raises(ValueError):
        decode_raw_units(b" " * (2 * 1024 * 1024 + 1), max_elements=10, max_depth=10)
    for maximum in (True, 0, 12 * 1024 * 1024 + 1):
        with pytest.raises(ValueError):
            decode_raw_units(b"<xbrl/>", max_elements=10, max_depth=10, max_bytes=maximum)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_xml_declarations_rejected_independent_of_encoding(encoding):
    payload = (
        f'<!DOCTYPE xbrl [<!ENTITY money "iso4217:USD">]>'
        f'<xbrl xmlns="{X}" xmlns:iso4217="{I}">'
        '<unit id="u"><measure>&money;</measure></unit></xbrl>'
    ).encode(encoding)
    with pytest.raises(ValueError):
        decode_raw_units(payload, max_elements=20, max_depth=20)
