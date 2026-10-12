"""Write the ``.LibPkg`` package file that ties a SchLib and a PcbLib together.

Altium stores a library package as a small INI-style text file whose ``DocumentPath``
entries are resolved relative to the package itself. Keeping the paths flat (rather
than the ``SchLib\\`` / ``PCBLib\\`` layout the IntLib extractor produces) means the
three files can simply live in one folder.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE = """[Design]
Version=1.0
HierarchyMode=0
OutputPath=Project Outputs

[Document1]
DocumentPath={schlib}
DocumentUniqueId={schlib_uid}

[Document2]
DocumentPath={pcblib}
DocumentUniqueId={pcblib_uid}
"""


def _unique_id(path: Path) -> str:
    """Stable 8-character uppercase id, mirroring Altium's own scheme."""
    import hashlib

    digest = hashlib.sha1(str(path.name).encode("utf-8")).hexdigest()
    return digest[:8].upper()


def write_libpkg(path: Path, schlib: Path, pcblib: Path) -> Path:
    """Create ``path`` referencing *schlib* and *pcblib*."""
    path = Path(path)
    content = TEMPLATE.format(
        schlib=schlib.name,
        schlib_uid=_unique_id(schlib),
        pcblib=pcblib.name,
        pcblib_uid=_unique_id(pcblib),
    )
    path.write_text(content, encoding="utf-8")
    return path
