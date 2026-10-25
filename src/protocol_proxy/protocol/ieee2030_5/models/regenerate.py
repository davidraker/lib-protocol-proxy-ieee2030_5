"""Regenerate ``sep.py`` from the IEEE 2030.5 ``sep.xsd`` schema.

The schema comes from the standard's supplemental material, which is IEEE copyright and not in this repository:
obtain each edition from IEEE and point ``IEEE2030_5_SCHEMAS`` at a directory with one folder per edition named by
year (``2013/``, ``2018/``, ``2023/``), or pass ``--schema``; the newest edition found is used. xsdata renders it to dataclasses with the
schema's own class and field names, then this script folds three renderings of the 2023 schema that are not wire
structure, so the module keeps the shape the proxy was written against (the 2018 rendering) plus the 2023 elements:

* every root element is declared as an anonymous extension of its type adding the ``schemaVer`` attribute, which
  xsdata renders as ``X_1`` (the type) plus ``X(X_1)`` (the element); the type keeps its name and gains ``schemaVer``
  as an optional attribute;
* simple-content types (``PerCent``, ``PowerOfTenMultiplierType``, ``TimeType``, ``mRIDType``, the enumeration
  carriers, the 2023 ``*ControlType`` setpoint wrappers) are a text value with optional XML attributes; fields of
  those types take the value's scalar type, as the parser and serializer then read and write the text directly.
  The ``disabled`` attribute of the ``*ControlType`` wrappers is not represented (a disabled setpoint is one that is
  absent); the lenient parser ignores it;
* xsdata's safe name for the schema's ``type`` element (``type_value``) goes back to ``type``.

``from __future__ import annotations`` is prepended so that a field named like its type (``Notification.Resource``)
does not shadow the type when xsdata resolves annotations. The ``*_r2_3`` revision markers (``xs:any`` extension
points) stay as optional fields.

Run from the repository root (needs ``xsdata[cli]``)::

    python src/protocol_proxy/protocol/ieee2030_5/models/regenerate.py [--schema PATH/sep.xsd] [--out models/sep.py]
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMAS_ENV = 'IEEE2030_5_SCHEMAS'
REVISION_MARKER = re.compile(r'_r\d+_\d+$')
XSDATA_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<Config xmlns="http://pypi.org/project/xsdata" version="25.7">
  <Output maxLineLength="119" genericCollections="false" unionType="false">
    <Package>sep</Package>
    <Format repr="true" eq="true" order="false" unsafeHash="false" frozen="false" slots="false" kwOnly="false">dataclasses</Format>
    <Structure>single-package</Structure>
    <DocstringStyle>reStructuredText</DocstringStyle>
    <RelativeImports>false</RelativeImports>
    <CompoundFields defaultName="choice" useSubstitutionGroups="false" forceDefaultName="false" maxNameParts="3">false</CompoundFields>
    <WrapperFields>false</WrapperFields>
    <PostponedAnnotations>false</PostponedAnnotations>
    <UnnestClasses>false</UnnestClasses>
    <IgnorePatterns>false</IgnorePatterns>
    <IncludeHeader>false</IncludeHeader>
  </Output>
  <Conventions>
    <ClassName case="originalCase" safePrefix="type"/>
    <FieldName case="originalCase" safePrefix="value"/>
    <ConstantName case="screamingSnakeCase" safePrefix="value"/>
    <ModuleName case="snakeCase" safePrefix="mod"/>
    <PackageName case="snakeCase" safePrefix="pkg"/>
  </Conventions>
  <Substitutions/>
  <Extensions/>
</Config>
"""


def discover_schema() -> tuple[str, Path] | None:
    found = {}
    root = Path(os.environ[SCHEMAS_ENV]).expanduser() if os.environ.get(SCHEMAS_ENV) else None
    if root is not None and root.is_dir():
        for path in sorted(root.glob('*/**/sep.xsd')):
            edition = path.relative_to(root).parts[0]
            if '__MACOSX' not in path.parts and re.fullmatch(r'20\d\d', edition):
                found.setdefault(edition, path)
    if not found:
        return None
    edition = max(found)
    return edition, found[edition]


def run_xsdata(schema: Path, out_dir: Path) -> Path:
    (out_dir / '.xsdata.xml').write_text(XSDATA_CONFIG, encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 'xsdata', 'generate', str(schema), '--config', '.xsdata.xml'],
                            cwd=out_dir, capture_output=True, text=True)
    modules = sorted((p for p in out_dir.rglob('*.py') if p.name != '__init__.py'), key=lambda p: p.stat().st_size)
    if not modules:
        raise SystemExit(f'xsdata produced nothing (is xsdata[cli] installed?):\n{result.stderr}')
    return modules[-1]


class Field:
    def __init__(self, node: ast.AnnAssign):
        self.node = node
        self.name = node.target.id  # type: ignore[union-attr]
        self.annotation = ast.unparse(node.annotation)
        self.metadata: dict = {}
        self.default = None
        if isinstance(node.value, ast.Call):
            for kw in node.value.keywords:
                if kw.arg == 'metadata':
                    self.metadata = ast.literal_eval(kw.value)
                elif kw.arg in ('default', 'default_factory'):
                    self.default = (kw.arg, ast.unparse(kw.value))

    @property
    def kind(self) -> str:
        return self.metadata.get('type', 'Element')

    @property
    def inner(self) -> tuple[str, bool]:
        txt = self.annotation
        m = re.fullmatch(r'Optional\[(.+)\]', txt)
        if m:
            txt = m.group(1)
        m = re.fullmatch(r'[Ll]ist\[(.+)\]', txt)
        if m:
            return m.group(1), True
        return txt, False

    def render(self, name: str | None = None, inner: str | None = None, extra: dict | None = None) -> str:
        own, is_list = self.inner
        inner = inner or own
        annotation = f'list[{inner}]' if is_list else f'Optional[{inner}]'
        default = 'default_factory=list' if is_list else 'default=None'
        metadata = dict(self.metadata)
        for key, value in (extra or {}).items():
            metadata.setdefault(key, value)
        return f'    {name or self.name}: {annotation} = field(\n        {default},\n        metadata={metadata!r},\n    )'


class Klass:
    def __init__(self, node: ast.ClassDef):
        self.node = node
        self.name = node.name
        self.bases = [ast.unparse(b) for b in node.bases]
        self.fields = [Field(s) for s in node.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
        self.start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        self.end = node.end_lineno


def fold(source: str) -> tuple[str, dict]:
    tree = ast.parse(source)
    lines = source.splitlines()
    classes = {n.name: Klass(n) for n in tree.body if isinstance(n, ast.ClassDef)}

    def all_fields(k: Klass) -> list[Field]:
        out: list[Field] = []
        for b in k.bases:
            if b in classes:
                out += all_fields(classes[b])
        return out + k.fields

    def structural(k: Klass) -> list[Field]:
        return [f for f in all_fields(k) if not REVISION_MARKER.search(f.name) and f.kind not in ('Wildcard', 'Attributes')]

    wrappers = {name: k for name, k in classes.items()
                if any(f.name == 'value' for f in structural(k)) and all(f.name == 'value' or f.kind == 'Attribute' for f in structural(k))}

    def scalar_of(name: str, seen=frozenset()) -> tuple[str, dict]:
        value = next(f for f in all_fields(wrappers[name]) if f.name == 'value')
        inner, _ = value.inner
        if inner in wrappers and inner not in seen:
            return scalar_of(inner, seen | {name})
        return inner, {k: v for k, v in value.metadata.items() if k in ('format', 'max_length', 'min_inclusive', 'max_inclusive', 'pattern')}

    pairs = {name: k for name, k in classes.items()
             if k.bases == [f'{name}_1'] and {f.name for f in k.fields} <= {'schemaVer'} and f'{name}_1' in classes}

    edits: list[tuple[int, int, list[str]]] = []      # (start line, end line, replacement) 1-based inclusive
    # Field rewrites (wrapper types to scalars, schema names), in every surviving class.
    stats = {'wrappers': sorted(wrappers), 'element_pairs': sorted(pairs), 'fields_retyped': 0, 'fields_renamed': 0}
    for name, k in classes.items():
        if name in wrappers:
            continue
        for f in k.fields:
            inner, _ = f.inner
            new_name = None
            wire = f.metadata.get('name')
            if isinstance(wire, str) and wire != f.name and wire.isidentifier() and f.name == f'{wire}_value':
                new_name = wire
                stats['fields_renamed'] += 1
            if inner in wrappers:
                scalar, extra = scalar_of(inner)
                stats['fields_retyped'] += 1
                edits.append((f.node.lineno, f.node.end_lineno, [f.render(new_name, scalar, extra)]))
            elif new_name:
                edits.append((f.node.lineno, f.node.end_lineno, [f.render(new_name)]))
    # Element wrappers: drop X, rename X_1 and give it schemaVer.
    def meta_of(k: Klass) -> ast.ClassDef | None:
        return next((n for n in k.node.body if isinstance(n, ast.ClassDef) and n.name == 'Meta'), None)

    def meta_value(meta: ast.ClassDef | None, key: str):
        for stmt in (meta.body if meta is not None else []):
            if isinstance(stmt, ast.Assign) and ast.unparse(stmt.targets[0]) == key:
                return ast.literal_eval(stmt.value)
        return None

    for name, k in pairs.items():
        typ = classes[f'{name}_1']
        edits.append((k.start, k.end, []))
        header = lines[typ.node.lineno - 1].replace(f'class {name}_1', f'class {name}', 1)
        edits.append((typ.node.lineno, typ.node.lineno, [header]))
        if k.fields:
            edits.append((typ.end + 1, typ.end, [k.fields[0].render()]))     # insertion after the type's last line
        # The element class carried the XML namespace in its Meta; the type must keep it to be a root element.
        namespace = meta_value(meta_of(k), 'namespace')
        typ_meta = meta_of(typ)
        if namespace and meta_value(typ_meta, 'namespace') is None:
            if typ_meta is not None:
                edits.append((typ_meta.lineno + 1, typ_meta.lineno, [f'        namespace = {namespace!r}']))
            else:
                first = typ.node.body[0]
                after = first.end_lineno if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) else typ.node.lineno
                edits.append((after + 1, after, ['    class Meta:', f'        namespace = {namespace!r}', '']))
    # Simple-content wrapper classes disappear.
    for name, k in wrappers.items():
        edits.append((k.start, k.end, []))
    # Apply bottom-up; at the same position a replacement or deletion goes first so an insertion does not shift it.
    for start, end, replacement in sorted(edits, key=lambda e: (-e[0], 0 if e[1] >= e[0] else 1)):
        lines[start - 1:end] = replacement
    text = '\n'.join(lines) + '\n'
    for name in pairs:
        text = re.sub(rf'\b{re.escape(name)}_1\b', name, text)
    text = 'from __future__ import annotations\n\n' + text
    return text, stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--schema', type=Path, default=None)
    parser.add_argument('--out', type=Path, default=HERE / 'sep.py')
    args = parser.parse_args(argv)
    edition = None
    schema = args.schema
    if schema is None:
        found = discover_schema()
        if found is None:
            raise SystemExit(f'No sep.xsd found: set {SCHEMAS_ENV} (one folder per edition, named by year) or pass --schema.')
        edition, schema = found
    module = run_xsdata(schema, Path(tempfile.mkdtemp(prefix='sep_xsdata_')))
    text, stats = fold(module.read_text(encoding='utf-8'))
    ast.parse(text)
    digest = hashlib.sha256(schema.read_bytes()).hexdigest()[:12]
    header = (f'# Generated by models/regenerate.py from the IEEE 2030.5{"-" + edition if edition else ""} sep.xsd '
              f'({schema.name}, sha256 {digest}) via xsdata. Do not edit by hand; see SOURCE.md.\n')
    args.out.write_text(header + text, encoding='utf-8')
    n_classes = sum(1 for n in ast.parse(text).body if isinstance(n, ast.ClassDef))
    print(f'{args.out}: {n_classes} classes; folded {len(stats["wrappers"])} simple-content wrappers and '
          f'{len(stats["element_pairs"])} element wrappers; {stats["fields_retyped"]} fields retyped, {stats["fields_renamed"]} renamed')


if __name__ == '__main__':
    main()
