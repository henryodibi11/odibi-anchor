"""Guard: repository content stays domain-neutral.

Examples, guidance and fixtures must not identify the owner's employer, people, internal
systems, or the energy-industry domain. This test tokenizes every tracked text file (and the
XML members of tracked zip containers such as .xlsx) and fails when a denylisted token appears.
Third-party snapshots under ``.assistant/references/snapshots/`` are excluded.

The denylist stores SHA-256 digests of lower-cased tokens, never the plaintext terms. A token is
a lower-cased run of ``[a-z0-9]`` parts joined by ``_`` or ``-``; the whole run and every
contiguous sub-span of up to four parts are checked, so ``prefix_<token>`` cannot hide a term.

To add a token, compute its digest and append it under a neutral category comment (never write
the term itself in this file)::

    python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].lower().encode()).hexdigest())" <token>
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_PREFIXES = (".assistant/references/snapshots/",)
MAX_SPAN_PARTS = 4

DENYLIST_SHA256 = frozenset({
    # personal or employer identifiers
    "5e0176c9d2070a5a2a22bf74b4abed303654690d58d64221ccbd022af827abc4",
    "952868609bf256be5cc244fad1bf5de01ff5664e839bc02c144a2523ec346872",
    "e32717af922ec4f45732a082449ca805e99a3a19d6bb3698078b20e3b0714489",
    "b4c4f99f3b2fe1d7095ad9f39a3b7ce1736c1c9d9904f7c6e484dd2e10bf77ea",
    "605ededd8ce908aad9ffe490c2238736563ab417680e63346bdc19876fa9625e",
    "3ccbd9105a45d8fcd4a0101c6532c599f6f59cfa4d4ce378792f547a869a4bea",
    # internal catalog and schema names
    "5af0c8d716a83273dc77aee0e84bcaf328f02526cc013ee46fa993fc71870a41",
    "cbade50ee7d5ca5bb6c82804dc5184a4b9205754fa9182b507753b6b9e06f350",
    "6784ac6b9758f20f4566cb23bd47140122e2000f0608b22317f152282e001f5d",
    "28700dbe9a45095d99fb8f49abb262408600e155b7410857cc01b02dd3a52ce6",
    "e601cf02d4efd6b2d77bd72002db431df930a05b1bcf9e3397cc9e0b36e6379c",
    "f502d12c289062a59164cd46caebe13a63280e994579e64e9f68fc11486933bb",
    # grid operator names
    "74fd829f39136cbc94a1ebd2253c44d0e3bd7333b6b896c57f707467e0057d9f",
    "914ccee57b252e420137fa3f665c67482e5d9a95bb860ce17bcc78e1a06ac653",
    "c5db03c1b6dc9eee8482e1eeccd13b0d329c8a9f3fb5a74513a1b9202ee2a647",
    "16affacdd2cae3488ae8141c1c65ecf6915d3894a1a2a02c8a7fa40b6d7af5e1",
    "02f7cbec51895afa3c7b621ab752009ce0cecac844791fabb3d3da12395965b2",
    "798b3ab7f95ee6a72bb52bd3711683a4cda736a05d56675b16846e829b63c992",
    # grid-connection request vocabulary
    "e13d31dd36c5b5fcc846d96d11413e0a601dc3df1251cdbda5c32bc725b18016",
    "7e88f6b74042179454f49b1b0943c6c5b0a98e9bd5858b4406b684b4fe8775f7",
    "9cb87871a7392bb8eb14340b3a9eb512defb005c003ed1635694935c8c4aee45",
    "dc682a08f75f50cd81d44645fbc14f7f5cb8763fbaca0544ebd91fb4afc9f181",
    "dca3ea16da5880bb9082bb60a902457d8c76267af0113edb73573a116252f6d2",
    "3ffac3c9370413a3a5ac9963de2c2c61d3abfae78a033d8a392452b714b54d6a",
    "92ba41b1eafd706599aae443713cd902bba48a10c95c775f66c83a4944d2d44c",
    "5327f0d617c1d66f5b74df9eacadab1fc25262b88eead657fdc8a507135589c1",
    "f8a2432a61108c2f892668124c93a9bb33c25a0b3a29a9d2d285491da6f19dcd",
    "64a30deae6d159f139ee8d9455863817648392dde317cbc44cb2757bbaaf0cde",
    "0b5d1985ed73b2d6ff5eda088abb9884b721807d61929fa72e26c2c78f007f36",
    "77ac1d4d815e3171e634edc3e2e88f1b42d73dd096c3913706ecb6bba41faeaf",
    "a21f28a4ac0b1de2557da8b07f3541dd07db656ebafdd29514db831786a99e19",
    "69299d5fd965e096cab76cc040c70eb7b447ff7ae72b43317752250e88d98cac",
    # power-capacity vocabulary
    "ab9bc75664d7032b2b24d757525603842b46f1f636739154551bda93308f9a25",
    "8e17371c330f0b0add506bdd11c666a7a79596bf96615bfd757108bdf549d0a4",
    "d0afa63c726b0a44fd8f67985c2f78ddf9410a308f591ae58f5e667839f07b01",
    "a3368c37c9912450f0d7f3117a1d059fc7dd2ccd0fe7e8332b8a159d01656f9d",
    "93acacd17d94a4d524148b177a481b12510dc32e6945fcec2e5fad2b56bb95ff",
    "cddb2e9000c8afd4663360cfe545fb308df301a3d39ccd357a8b29691a9a8126",
    "5dd95f76a15599a820a2507fd1ce8e6cde8bc30ef81f154b2a6806fd0f3c405f",
    "cf0c7caa1cc55779e7ef489de20ba3c79aceda4fb91dd6d4ad042b23f91d2f71",
    "34b47b460d357bfde69f333b74c61682760f1ac3a66f763d508df1fc795a1231",
    # utility company short names
    "f7c1fb1894e63838c7a2e783346a657241b00050d5e93a28206141f62dcbb3e8",
    "57cd285cc10d8e5b3f87526e9bb4b145a97ed5d1815f512b170f0c19ea6d7b29",
    "ad589ceb8b5ccca048911a0e4dcf5466a1f14dc9f50517c9a2c53988d7de2521",
    "f1e61e4389fcfb3be8effddb4aca8d74ebc98daabb8f52b11dfdb2e91138283e",
    "65a7ad5a1b5d1498a03145ff144a719c0b0b25198b0e9ab2e892d8bcef4ca387",
    "d2754787111355741559612ec8647daf118e3da21dc7146fbe0abaf4229ccb80",
    "76e4952ce4de5226bdeb05a9935eb9c33e2eac2a95640f2b260fd7e2870d54a8",
    "b11be1856f3813f54e75311b0a5c066dccc94136f550852c6d189fb3363b30b2",
})

# Product routing behavior awaiting an owner decision; exempt only these digests in these files.
EXEMPTIONS: dict[str, frozenset[str]] = {
    "src/odibi_anchor/codebase/_memory_db.py": frozenset({
        "b4c4f99f3b2fe1d7095ad9f39a3b7ce1736c1c9d9904f7c6e484dd2e10bf77ea",
        "605ededd8ce908aad9ffe490c2238736563ab417680e63346bdc19876fa9625e",
    }),
    "tests/codebase/test_memory_db.py": frozenset({
        "b4c4f99f3b2fe1d7095ad9f39a3b7ce1736c1c9d9904f7c6e484dd2e10bf77ea",
        "605ededd8ce908aad9ffe490c2238736563ab417680e63346bdc19876fa9625e",
    }),
}

_RUN = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")
_PART = re.compile(r"[a-z0-9]+|[_-]")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for run in _RUN.findall(text.lower()):
        pieces = _PART.findall(run)
        words = pieces[0::2]
        for start in range(len(words)):
            for size in range(1, min(MAX_SPAN_PARTS, len(words) - start) + 1):
                tokens.add("".join(pieces[2 * start:2 * (start + size) - 1]))
    return tokens


def _texts(data: bytes) -> list[str]:
    if b"\0" not in data:
        return [data.decode("utf-8", errors="replace")]
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist() if name.endswith((".xml", ".rels"))]


def _tracked_files() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"tracked-file listing unavailable outside a Git checkout: {exc}")
    return [path for path in result.stdout.decode().split("\0")
            if path and not path.startswith(EXCLUDED_PREFIXES)]


def _violations(path: str, data: bytes) -> list[str]:
    allowed = EXEMPTIONS.get(path, frozenset())
    found = set()
    for text in _texts(data):
        found |= {_digest(token) for token in _tokens(text)} & DENYLIST_SHA256
    return sorted(found - allowed)


def test_tokenizer_checks_identifier_sub_spans():
    tokens = _tokens("gold.Orders_Web_North_v2 and data-load-job")
    assert {"orders_web", "web_north", "north", "orders_web_north_v2", "load-job"} <= tokens
    assert "orders.web" not in tokens


def test_tracked_files_contain_no_denylisted_tokens():
    offenders = {}
    for path in _tracked_files():
        file_path = ROOT / path
        if not file_path.is_file():
            continue
        hits = _violations(path, file_path.read_bytes())
        if hits:
            offenders[path] = [digest[:12] for digest in hits]
    assert not offenders, (
        "Denylisted domain or identity tokens found (digest prefixes shown; recompute candidate "
        f"tokens with the recipe in this module's docstring): {offenders}"
    )
