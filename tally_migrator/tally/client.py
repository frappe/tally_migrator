import requests
import xml.etree.ElementTree as ET
from dataclasses import dataclass
import frappe


@dataclass
class TallyConfig:
    host:             str = "localhost"
    port:             int = 9000
    tally_company:    str = ""   # Company name inside Tally
    erpnext_company:  str = ""   # Company name inside ERPNext
    timeout:          int = 30

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class TallyClient:
    """
    Communicates with Tally via its built-in XML/HTTP server (port 9000).

    Tally must be open with the HTTP server enabled:
      F12 → Advanced Configuration → Enable ODBC Server → Port 9000
    """

    HEADERS = {"Content-Type": "application/xml; charset=utf-8"}

    def __init__(self, config: TallyConfig):
        self.config   = config
        self._session = requests.Session()
        self._session.headers.update(self.HEADERS)

    # ── Public API ────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Returns True if Tally is reachable."""
        try:
            self._post(self._company_list_xml())
            return True
        except Exception:
            return False

    def get_companies(self) -> list[str]:
        """List all companies currently loaded in Tally.

        Uses a Collection request of TYPE Company (the reliable enumeration
        method). The response wraps each company in a <COMPANY NAME="...">
        element with a <NAME> child; we read both and fall back across the
        tag variants different Tally Prime builds emit.
        """
        root = self._post(self._company_list_xml())
        names: list[str] = []
        for comp in root.iter("COMPANY"):
            name = (comp.get("NAME") or comp.findtext("NAME") or "").strip()
            if name and name not in names:
                names.append(name)
        # Fallback for builds that return <COMPANYNAME> under a report export.
        if not names:
            names = [c.text.strip() for c in root.iter("COMPANYNAME") if c.text]
        return names

    def raw_companies(self) -> str:
        """Return the raw XML Tally sends for the company-list request.

        Diagnostic only — used to inspect what a specific Tally build emits.
        """
        resp = self._session.post(
            self.config.url,
            data=self._company_list_xml().encode("utf-8"),
            timeout=self.config.timeout,
        )
        return resp.text

    def get_collection(self, obj_type: str, fields: list[str]) -> list[dict]:
        """
        Generic master fetcher.
        obj_type: 'Ledger' | 'Group' | 'Stock Item' | 'Godown'
        fields:   TDL field names to fetch per record.
        Returns list of dicts keyed by field name + '_name' for the master name.
        """
        xml  = self._collection_xml(obj_type, fields)
        root = self._post(xml)
        tag  = obj_type.upper().replace(" ", "")
        return self._parse_collection(root, tag, fields)

    # ── XML Builders ──────────────────────────────────────────────────────────

    def _company_list_xml(self) -> str:
        # Collection of TYPE Company is the reliable way to enumerate the
        # companies open in Tally. ISMODIFY="No" keeps it read-only; the
        # response returns <COMPANY NAME="..."><NAME>...</NAME></COMPANY>.
        return (
            "<ENVELOPE>"
            "<HEADER>"
            "<VERSION>1</VERSION>"
            "<TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Collection</TYPE>"
            "<ID>CompanyList</ID>"
            "</HEADER>"
            "<BODY><DESC>"
            "<STATICVARIABLES>"
            "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
            "</STATICVARIABLES>"
            "<TDL><TDLMESSAGE>"
            '<COLLECTION NAME="CompanyList" ISMODIFY="No">'
            "<TYPE>Company</TYPE>"
            "<NATIVEMETHOD>NAME</NATIVEMETHOD>"
            "</COLLECTION>"
            "</TDLMESSAGE></TDL>"
            "</DESC></BODY>"
            "</ENVELOPE>"
        )

    def _collection_xml(self, obj_type: str, fields: list[str]) -> str:
        fetch = ", ".join(fields)
        return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA>
    <REQUESTDESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{self.config.tally_company}</SVCURRENTCOMPANY>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <REPORTNAME>MigratorCol</REPORTNAME>
    </REQUESTDESC>
    <REQUESTDATA>
      <TALLYRPTNAMESET>
        <COLLECTIONNAME>MigratorCol</COLLECTIONNAME>
        <TDLMESSAGE>
          <COLLECTION NAME="MigratorCol" ISMODIFY="No">
            <TYPE>{obj_type}</TYPE>
            <FETCH>{fetch}</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TALLYRPTNAMESET>
    </REQUESTDATA>
  </EXPORTDATA></BODY>
</ENVELOPE>"""

    # ── HTTP + Parsing ────────────────────────────────────────────────────────

    def _post(self, xml: str) -> ET.Element:
        try:
            resp = self._session.post(
                self.config.url,
                data=xml.encode("utf-8"),
                timeout=self.config.timeout,
            )
            resp.raise_for_status()
            return ET.fromstring(resp.content)
        except requests.ConnectionError:
            frappe.throw(f"Cannot connect to Tally at {self.config.url}. Is Tally open?")
        except requests.Timeout:
            frappe.throw("Tally connection timed out. Try again.")
        except ET.ParseError as e:
            frappe.throw(f"Tally returned malformed XML: {e}")

    def _parse_collection(self, root: ET.Element, tag: str, fields: list[str]) -> list[dict]:
        records = []
        for elem in root.iter(tag):
            name = elem.get("NAME", "").strip()
            if not name:
                continue
            record = {"_name": name}
            for f in fields:
                xml_tag       = f.upper().replace(" ", "")
                record[f]     = (elem.findtext(xml_tag) or "").strip()
            records.append(record)
        return records
