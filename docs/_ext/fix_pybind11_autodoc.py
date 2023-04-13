# pybind11 generates some docstrings and function signatures that are functionally
# correct but encourage users to rely on implementation details. Fix these here.

from __future__ import annotations

import re

replacements = [
    (re.compile(r'pikepdf._core.(\w+)\b'), r'pikepdf.\1'),
    (re.compile(r'QPDFTokenizer::Token\b'), 'pikepdf.Token'),
    (re.compile(r'QPDFEFStreamObjectHelper'), 'pikepdf._core.AttachedFile'),
    (re.compile(r'QPDFObjectHandle::TokenFilter'), 'pikepdf.TokenFilter'),
    (re.compile(r'QPDFObjectHandle::Rectangle'), 'pikepdf.Rectangle'),
    (re.compile(r'QPDFObjectHandle'), 'pikepdf.Object'),
    (re.compile(r'QPDFExc'), 'pikepdf.PdfError'),
    (re.compile(r'QPDFPageObjectHelper'), 'pikepdf.Page'),
]


def fix_sigs(app, what, name, obj, options, signature, return_annotation):
    for from_, to in replacements:
        if signature:
            signature = from_.sub(to, signature)
        if return_annotation:
            return_annotation = from_.sub(to, return_annotation)
    return signature, return_annotation


def fix_doc(app, what, name, obj, options, lines):
    for n, line in enumerate(lines[:]):
        s = line
        for from_, to in replacements:
            s = from_.sub(to, s)
        lines[n] = s


def setup(app):
    app.connect('autodoc-process-signature', fix_sigs)
    app.connect('autodoc-process-docstring', fix_doc)

    return {'version': '0.1', 'parallel_read_safe': True, 'parallel_write_safe': True}
