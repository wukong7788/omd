# Observed SEC source admission: resource experiment

Status: cap expansion rejected; SGML scanner optimization ACCEPTED (independent Astra review).

The representative pilot encountered a source larger than the SDK's 8 MiB
admission limit. An offline experiment considered explicit 32 MiB source and
64 MiB aggregate dependency admission. **Neither expansion is implemented.**
The observed and traditional producers retain their 8 MiB source maximum;
observed bundles retain 8 MiB per dependency and 32 MiB aggregate maximum.
Existing component/XML/row limits are unchanged. No live retry was authorized
by these experiments, and the earlier pilot remains failed.

## Evidence and decision

All following probes use synthetic retained sources, the actual pinned parser,
production, bundle write and complete bundle load. They run in isolated macOS
processes with network connections denied. Reported memory is process lifetime
peak RSS, including fixture setup, dependency caches and repeated producer reads;
it is not tracemalloc. No financial PASS or publication is inferred.

| Experimental source | Peak RSS bytes | Seconds | Result against 512 MiB / 60 s |
| --- | ---: | ---: | --- |
| 32 MiB, one large UTF-8 document, original scanner | 713,310,208 | 1.868 | Fail |
| 32 MiB, one large UTF-8 document, span scanner | 445,022,208 | 1.353 | Pass for this sample only |
| 32 MiB, 64 documents with astral text | 713,228,288 | 1.477 | Fail |
| 16 MiB, 64 documents with astral text | 444,350,464 | 0.783 | Pass for this sample only |
| 16 MiB, large recognized XBRL document, span wrapper | 310,099,968 | 0.708 | Pass for this sample only |
| Two sources of 16,760,832 bytes each, near 32 MiB aggregate | 808,665,088 | 1.459 | Fail |

The one-source results do not establish a safe multi-source budget. Reducing
only the per-source cap to 16 MiB would therefore not justify acceptance.
All expanded admission code was withdrawn. Reports are retained locally under
ignored `artifacts/sec-source-admission-acceptance/`; failed reports are not
relabelled as passes. A diagnostic tracemalloc run and explicit garbage
collection did not resolve the measured repeated-build RSS growth; no precise
allocator or third-party leak root cause is claimed.

## Retained optimization

The SGML scanner uses source spans rather than full document/text copies.
Observed production validates unused XBRL wrappers by span, while traditional
production still returns its extracted components. Full UTF-8 decoding and the
pinned FilingSGML parser remain in the chain. TYPE/TEXT uniqueness, nesting,
document counts, duplicate components, Unicode whitespace and wrapper semantics
remain unchanged. Admission budgets and financial/parser identities are unchanged.

The offline regressions include original 8 MiB admission boundaries, retained
v1/v2 output/fact/production and mixed-bundle golden identities, Unicode start
anchors, 64/65 document limits and malformed unused documents/wrappers. A seeded
10,000-case mutation comparison against the previous scanner found identical
acceptance and extracted components; this is supporting evidence, not exhaustive
proof. Subsequent smaller-source acquisition needs its own source-identity,
integrity and time-evidence contract, never a truncated source masquerading as
complete SGML.

Final unchanged-limit probe: one 8 MiB source with 64 documents and astral text
completed production plus bundle write/load in 0.377 seconds at 310,067,200 bytes
RSS. The affected 129-test suite and global Ruff/format/ty checks passed.
This does not establish a full-universe or all-input performance guarantee.
