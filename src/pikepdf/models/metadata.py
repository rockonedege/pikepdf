# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2018, James R. Barlow (https://github.com/jbarlow83/)

from collections.abc import MutableMapping
from datetime import datetime
from functools import wraps
from io import BytesIO
from pkg_resources import (
    get_distribution as _get_distribution,
    DistributionNotFound
)
import sys
from warnings import warn, filterwarnings
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import QName

from defusedxml.ElementTree import parse

from .. import Stream, Name, String

XMP_NS_DC = "http://purl.org/dc/elements/1.1/"
XMP_NS_PDF = "http://ns.adobe.com/pdf/1.3/"
XMP_NS_PDFA_ID = "http://www.aiim.org/pdfa/ns/id/"
XMP_NS_PDFX_ID = "http://www.npes.org/pdfx/ns/id/"
XMP_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS_XMP = "http://ns.adobe.com/xap/1.0/"
XMP_NS_XMP_MM = "http://ns.adobe.com/xap/1.0/mm/"

DEFAULT_NAMESPACES = [
    ('adobe:ns:meta/', 'x'),
    (XMP_NS_DC, 'dc'),
    (XMP_NS_PDF, 'pdf'),
    (XMP_NS_PDFA_ID, 'pdfaid'),
    (XMP_NS_RDF, 'rdf'),
    (XMP_NS_XMP, 'xmp'),
    (XMP_NS_XMP_MM, 'xapMM'),
]

for _uri, _prefix in DEFAULT_NAMESPACES:
    ET.register_namespace(_prefix, _uri)

XPACKET_BEGIN = b"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n"""

XMP_EMPTY = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="pikepdf">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
 </rdf:RDF>
</x:xmpmeta>
"""

XPACKET_END = b"""<?xpacket end="w"?>\n"""

TRIVIAL_XMP = (XPACKET_BEGIN + XMP_EMPTY + XPACKET_END)


# Repeat this to avoid circular from top package's pikepdf.__version__
try:
    pikepdf_version = _get_distribution(__name__).version
except DistributionNotFound:
    pikepdf_version = "unknown version"


def encode_pdf_date(d: datetime) -> str:
    """Encode Python datetime object as PDF date string

    From Adobe pdfmark manual:
    (D:YYYYMMDDHHmmSSOHH'mm')
    D: is an optional prefix. YYYY is the year. All fields after the year are
    optional. MM is the month (01-12), DD is the day (01-31), HH is the
    hour (00-23), mm are the minutes (00-59), and SS are the seconds
    (00-59). The remainder of the string defines the relation of local
    time to GMT. O is either + for a positive difference (local time is
    later than GMT) or - (minus) for a negative difference. HH' is the
    absolute value of the offset from GMT in hours, and mm' is the
    absolute value of the offset in minutes. If no GMT information is
    specified, the relation between the specified time and GMT is
    considered unknown. Regardless of whether or not GMT
    information is specified, the remainder of the string should specify
    the local time.
    """

    pdfmark_date_fmt = r'%Y%m%d%H%M%S'
    s = d.strftime(pdfmark_date_fmt)
    tz = d.strftime('%z')
    if tz == '':
        # Ghostscript <= 9.23 handles missing timezones incorrectly, so if
        # timezone is missing, move it into GMT.
        # https://bugs.ghostscript.com/show_bug.cgi?id=699182
        s += "+00'00'"
    else:
        sign, tz_hours, tz_mins = tz[0], tz[1:3], tz[3:5]
        s += "{}{}'{}'".format(sign, tz_hours, tz_mins)
    return s


def decode_pdf_date(s: str) -> datetime:
    """Decode a pdfmark date to a Python datetime object

    A pdfmark date is a string in a paritcular format. See the pdfmark
    Reference for the specification.
    """
    if isinstance(s, String):
        s = str(s)
    if s.startswith('D:'):
        s = s[2:]

    # Literal Z00'00', is incorrect but found in the wild,
    # probably made by OS X Quartz -- standardize
    if s.endswith("Z00'00'"):
        s = s.replace("Z00'00'", '+0000')
    elif s.endswith('Z'):
        s = s.replace('Z', '+0000')
    s = s.replace("'", "")  # Remove apos from PDF time strings
    try:
        return datetime.strptime(s, r'%Y%m%d%H%M%S%z')
    except ValueError:
        return datetime.strptime(s, r'%Y%m%d%H%M%S')


class AuthorConverter:
    @staticmethod
    def xmp_from_docinfo(docinfo_val):
        return [docinfo_val]

    @staticmethod
    def docinfo_from_xmp(xmp_val):
        if isinstance(xmp_val, str):
            return xmp_val
        else:
            return '; '.join(xmp_val)


if sys.version_info < (3, 7):
    def fromisoformat(datestr):
        import re
        # strptime %z can't parse a timezone with punctuation
        if re.search(r'[+-]\d{2}[-:]\d{2}$', datestr):
            datestr = datestr[:-3] + datestr[-2:]
        try:
            return datetime.strptime(datestr, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return datetime.strptime(datestr, "%Y-%m-%dT%H:%M:%S")
else:
    fromisoformat = datetime.fromisoformat

class DateConverter:
    @staticmethod
    def xmp_from_docinfo(docinfo_val):
        if docinfo_val == '':
            return ''
        return decode_pdf_date(docinfo_val).isoformat()

    @staticmethod
    def docinfo_from_xmp(xmp_val):
        dateobj = fromisoformat(xmp_val)
        return encode_pdf_date(dateobj)


def ensure_loaded(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        if not self._xmp:
            self._load()
        return fn(self, *args, **kwargs)
    return wrapper


class PdfMetadata(MutableMapping):
    """Read and edit the XMP metadata associated with a PDF

    Requires/relies on python-xmp-toolkit and libexempi.

    To update metadata, use a with block.

    .. code-block:: python

        with pdf.open_metadata() as records:
            records['dc:title'] = 'New Title'

    See Also:
        :meth:`pikepdf.Pdf.open_metadata`
    """

    DOCINFO_MAPPING = [
        (XMP_NS_DC, 'creator', Name.Author, AuthorConverter),
        (XMP_NS_DC, 'description', Name.Subject, None),
        (XMP_NS_DC, 'title', Name.Title, None),
        (XMP_NS_PDF, 'Keywords', Name.Keywords, None),
        (XMP_NS_PDF, 'Producer', Name.Producer, None),
        (XMP_NS_XMP, 'CreateDate', Name.CreationDate, DateConverter),
        (XMP_NS_XMP, 'CreatorTool', Name.Creator, None),
        (XMP_NS_XMP, 'ModifyDate', Name.ModDate, DateConverter),
    ]

    NS = {prefix: uri for uri, prefix in DEFAULT_NAMESPACES}
    REVERSE_NS = {uri: prefix for uri, prefix in DEFAULT_NAMESPACES}

    def __init__(self, pdf, pikepdf_mark=True, sync_docinfo=True):
        self._pdf = pdf
        self._xmp = None
        self.mark = pikepdf_mark
        self.sync_docinfo = sync_docinfo
        self._updating = False

    def _create_xmp(self):
        self._xmp = parse(BytesIO(TRIVIAL_XMP))

    def load_from_docinfo(self, docinfo, delete_missing=False):
        """Populate the XMP metadata object with DocumentInfo

        A few entries in the deprecated DocumentInfo dictionary are considered
        approximately equivalent to certain XMP records. This method copies
        those entries into the XMP metadata.
        """
        for uri, shortkey, docinfo_name, converter in self.DOCINFO_MAPPING:
            qname = QName(uri, shortkey)
            # docinfo might be a dict or pikepdf.Dictionary, so lookup keys
            # by str(Name)
            val = docinfo.get(str(docinfo_name))
            if val is None:
                if delete_missing and qname in self:
                    del self[qname]
                continue
            val = str(val)
            if converter:
                val = converter.xmp_from_docinfo(val)
            if not val:
                continue
            self[qname] = val

    def _load(self):
        try:
            data = BytesIO(self._pdf.Root.Metadata.get_stream_buffer())
        except AttributeError:
            self._create_xmp()
        else:
            self._xmp = parse(data)

    @ensure_loaded
    def __enter__(self):
        self._updating = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                return
            self._apply_changes()
        finally:
            self._updating = False

    def _update_docinfo(self):
        """Update the PDF's DocumentInfo dictionary to match XMP metadata

        The standard mapping is described here:
            https://www.pdfa.org/pdfa-metadata-xmp-rdf-dublin-core/
        """
        self._pdf.docinfo  # Touch object to ensure it exists
        for uri, element, docinfo_name, converter in self.DOCINFO_MAPPING:
            qname = QName(uri, element)
            try:
                value = self[qname]
            except KeyError:
                if docinfo_name in self._pdf.docinfo:
                    del self._pdf.docinfo[docinfo_name]
                continue
            if converter:
                value = converter.docinfo_from_xmp(value)
            try:
                # Try to save pure ASCII
                self._pdf.docinfo[docinfo_name] = value.encode('ascii')
            except UnicodeEncodeError:
                # qpdf will serialize this as a UTF-16 with BOM string
                self._pdf.docinfo[docinfo_name] = value

    def _get_xml_bytes(self, xpacket=True):
        data = BytesIO()
        if xpacket:
            data.write(XPACKET_BEGIN)
        self._xmp.write(data, encoding='utf-8')
        if xpacket:
            data.write(XPACKET_END)
        data.seek(0)
        return data.read()

    def _apply_changes(self):
        """Serialize our changes back to the PDF in memory

        Depending how we are initialized, leave our metadata mark and producer.
        """
        if self.mark:
            self[QName(XMP_NS_XMP, 'MetadataDate')] = datetime.now().isoformat()
            self[QName(XMP_NS_PDF, 'Producer')] = 'pikepdf ' + pikepdf_version
        xml = self._get_xml_bytes()
        self._pdf.Root.Metadata = Stream(self._pdf, xml)
        self._pdf.Root.Metadata[Name.Type] = Name.Metadata
        self._pdf.Root.Metadata[Name.Subtype] = Name.XML
        if self.sync_docinfo:
            self._update_docinfo()

    def _qname(self, name):
        """Convert name to an XML QName

        e.g. pdf:Producer -> {http://ns.adobe.com/pdf/1.3/}Producer
        """
        if isinstance(name, QName):
            return name
        if not isinstance(name, str):
            raise TypeError("{} must be str".format(name))
        if name == '':
            return name
        if name.startswith('{'):
            return name
        prefix, tag = name.split(':', maxsplit=1)
        uri = self.NS[prefix]
        return QName(uri, tag)

    def _prefix_from_uri(self, uriname):
        """Given a fully qualified XML name, find a prefix

        e.g. {http://ns.adobe.com/pdf/1.3/}Producer -> pdf:Producer
        """
        uripart, tag = uriname.split('}', maxsplit=1)
        uri = uripart.replace('{', '')
        return self.REVERSE_NS[uri] + ':' + tag

    def _get_subelements(self, node):
        """Gather the sub-elements attached to a node

        Gather rdf:Bag and and rdf:Seq into set and list respectively. For
        alternate languages values, take the first language only for
        simplicity.
        """
        items = node.find('rdf:Alt', self.NS)
        if items:
            return items[0].text

        CONTAINERS = [
            ('Bag', set, set.add),
            ('Seq', list, list.append),
        ]
        for xmlcontainer, container, insertfn in CONTAINERS:
            items = node.find('rdf:{}'.format(xmlcontainer), self.NS)
            if not items:
                continue
            result = container()
            for item in items:
                insertfn(result, item.text)
            return result
        return ''

    def _get_elements(self, name=''):
        """Get elements from XMP

        Core routine to find elements matching name within the XMP and yield
        them.

        For XMP spec 7.9.2.2, rdf:Description with property attributes,
        we yield the node which will have the desired as one of its attributes.
        qname is returned so that the node.attrib can be used to locate the
        source.

        For XMP spec 7.5, simple valued XMP properties, we yield the node,
        None, and the value. For structure or array valued properties we gather
        the elements. We ignore qualifiers.

        Args:
            name (str): a prefixed name or QName to look for within the
                data section of the XMP; looks for all data keys if omitted

        Yields:
            tuple: (node, qname_attrib, value, parent_node)

        """
        qname = self._qname(name)
        rdf = self._xmp.find('.//rdf:RDF', self.NS)
        for rdfdesc in rdf.findall('rdf:Description[@rdf:about=""]', self.NS):
            if qname and qname in rdfdesc.keys():
                yield (rdfdesc, qname, rdfdesc.get(qname), rdf)
            elif not qname:
                for k, v in rdfdesc.items():
                    if v:
                        yield (rdfdesc, k, v, rdf)
            for node in rdfdesc.findall('.//{}'.format(qname), self.NS):
                if node.text and node.text.strip():
                    yield (node, None, node.text, rdfdesc)
                    continue
                values = self._get_subelements(node)
                yield (node, None, values, rdfdesc)

    def _get_element_values(self, name=''):
        yield from (v[2] for v in self._get_elements(name))

    @ensure_loaded
    def __contains__(self, key):
        try:
            return any(self._get_element_values(key))
        except KeyError:
            return False

    @ensure_loaded
    def __getitem__(self, key):
        try:
            return next(self._get_element_values(key))
        except StopIteration:
            raise KeyError(key)

    @ensure_loaded
    def __iter__(self):
        for node, attrib, _val, _parents in self._get_elements():
            if attrib:
                yield attrib
            else:
                yield node.tag

    @ensure_loaded
    def __len__(self):
        return len(list(iter(self)))

    @ensure_loaded
    def __setitem__(self, key, val):
        if isinstance(val, str):
            val = val.replace('\x00', '')
        if not self._updating:
            raise RuntimeError("Metadata not opened for editing, use with block")
        try:
            # Locate existing node to replace
            node, attrib, _oldval, parent = next(self._get_elements(key))
            if attrib:
                if not isinstance(val, str):
                    raise TypeError(val)
                node.set(attrib, val)
            elif isinstance(val, list):
                for child in node.findall('*'):
                    node.remove(child)
                seq = ET.SubElement(node, QName(XMP_NS_RDF, 'Seq'))
                for subval in val:
                    el = ET.SubElement(seq, QName(XMP_NS_RDF, 'li'))
                    el.text = subval
            elif isinstance(val, str):
                for child in node.findall('*'):
                    node.remove(child)
                node.text = val
            else:
                raise TypeError(val)
        except StopIteration:
            # Insert a new node (with property attribute)
            rdf = self._xmp.find('.//rdf:RDF', self.NS)
            if isinstance(val, list):
                rdfdesc = ET.SubElement(
                    rdf, QName(XMP_NS_RDF, 'Description'),
                    attrib={
                        QName(XMP_NS_RDF, 'about'): '',
                    },
                )
                node = ET.SubElement(rdfdesc, self._qname(key))
                seq = ET.SubElement(node, QName(XMP_NS_RDF, 'Seq'))
                for subval in val:
                    el = ET.SubElement(seq, QName(XMP_NS_RDF, 'li'))
                    el.text = subval
            elif isinstance(val, str):
                rdfdesc = ET.SubElement(
                    rdf, QName(XMP_NS_RDF, 'Description'),
                    attrib={
                        QName(XMP_NS_RDF, 'about'): '',
                        self._qname(key): val
                    },
                )

    @ensure_loaded
    def __delitem__(self, key):
        if not self._updating:
            raise RuntimeError("Metadata not opened for editing, use with block")
        try:
            node, attrib, _oldval, parent = next(self._get_elements(key))
            if attrib:  # Inline
                # TODO multiple attribs?
                pass
            parent.remove(node)
        except StopIteration:
            raise KeyError(key)

    @property
    @ensure_loaded
    def pdfa_status(self):
        """Returns the PDF/A conformance level claimed by this PDF, or False

        A PDF may claim to PDF/A compliant without this being true. Use an
        independent verifier such as veraPDF to test if a PDF is truly
        conformant.

        Returns:
            str: The conformance level of the PDF/A, or an empty string if the
            PDF does not claim PDF/A conformance. Possible valid values
            are: 1A, 1B, 2A, 2B, 2U, 3A, 3B, 3U.
        """
        key_part = QName(XMP_NS_PDFA_ID, 'part')
        key_conformance = QName(XMP_NS_PDFA_ID, 'conformance')
        try:
            return self[key_part] + self[key_conformance]
        except KeyError:
            return ''

    @property
    @ensure_loaded
    def pdfx_status(self):
        """Returns the PDF/X conformance level claimed by this PDF, or False

        A PDF may claim to PDF/X compliant without this being true. Use an
        independent verifier such as veraPDF to test if a PDF is truly
        conformant.

        Returns:
            str: The conformance level of the PDF/X, or an empty string if the
            PDF does not claim PDF/X conformance.
        """
        pdfx_version = QName(XMP_NS_PDFX_ID, 'GTS_PDFXVersion')
        try:
            return self[pdfx_version]
        except KeyError:
            return ''

    def __str__(self):
        return self._get_xml_bytes(xpacket=False).decode('utf-8')
