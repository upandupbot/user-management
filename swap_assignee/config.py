"""Configuration loading: config.ini + env-var overrides + secret masking."""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_API_URL = "https://api.pipefy.com/graphql"
DEFAULT_PAPERTRAIL_HOST = "logs.papertrailapp.com"


@dataclass
class Config:
    pipefy_api_url: str
    pipefy_api_token: str
    source: str = "up-and-up"
    org_id: Optional[str] = None
    papertrail_token: Optional[str] = None
    papertrail_host: Optional[str] = None
    papertrail_port: Optional[int] = None
    audit_webhook_url: Optional[str] = None
    audit_sheet_id: Optional[str] = None
    audit_service_account_file: Optional[str] = None
    audit_service_account_json: Optional[str] = None
    web_password: Optional[str] = None
    web_secret_key: Optional[str] = None

    def mask_token(self) -> str:
        return mask_secret(self.pipefy_api_token)


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _clean(value):
    """Strip whitespace and any trailing inline comment (`;` or `#`)."""
    if value is None:
        return None
    s = str(value)
    # Cut at the first ' ;' or ' #' (require whitespace so URLs with # are safe).
    for marker in (" ;", "\t;", " #", "\t#"):
        i = s.find(marker)
        if i != -1:
            s = s[:i]
    return s.strip() or None


def load_config(path: str | os.PathLike[str] = "config.ini") -> Config:
    # inline_comment_prefixes makes configparser strip `; ...` / `# ...` from values.
    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    p = Path(path)
    if p.exists():
        parser.read(p, encoding="utf-8")

    pipefy = parser["Pipefy"] if parser.has_section("Pipefy") else {}
    paper = parser["PaperTrail"] if parser.has_section("PaperTrail") else {}
    audit = parser["Audit"] if parser.has_section("Audit") else {}
    web = parser["Web"] if parser.has_section("Web") else {}

    api_url = _clean(pipefy.get("pipefy_api_url") if pipefy else None) or DEFAULT_API_URL
    # Defensive: the brief notes app.pipefy.com/graphql is wrong; rewrite it.
    if "app.pipefy.com" in api_url:
        api_url = DEFAULT_API_URL

    token = os.environ.get("PIPEFY_API_TOKEN") or _clean(pipefy.get("pipefy_api_token") if pipefy else None) or ""
    source = _clean(pipefy.get("source") if pipefy else None) or "up-and-up"
    org_id = _clean(pipefy.get("org_id") if pipefy else None)

    pt_token = os.environ.get("PAPERTRAIL_LOG_TOKEN") or _clean(paper.get("papertrail_log_token") if paper else None)
    pt_host = _clean(paper.get("papertrail_host") if paper else None) or DEFAULT_PAPERTRAIL_HOST
    pt_port_raw = _clean(paper.get("papertrail_port") if paper else None)
    pt_port = int(pt_port_raw) if pt_port_raw else None

    audit_webhook = (
        os.environ.get("SWAP_AUDIT_WEBHOOK_URL")
        or _clean(audit.get("webhook_url") if audit else None)
    )
    audit_sheet_id = (
        os.environ.get("SWAP_AUDIT_SHEET_ID")
        or _clean(audit.get("sheet_id") if audit else None)
    )
    audit_sa_file = (
        os.environ.get("SWAP_AUDIT_SERVICE_ACCOUNT_FILE")
        or _clean(audit.get("service_account_file") if audit else None)
    )
    # Raw JSON key content for hosted deployments where there is no key file
    # on disk (paste the whole key file into this env var).
    audit_sa_json = os.environ.get("SWAP_AUDIT_SERVICE_ACCOUNT_JSON")

    web_password = os.environ.get("SWAP_WEB_PASSWORD") or _clean(web.get("password") if web else None)
    web_secret_key = os.environ.get("SWAP_WEB_SECRET_KEY") or _clean(web.get("secret_key") if web else None)

    return Config(
        pipefy_api_url=api_url,
        pipefy_api_token=token,
        source=source,
        org_id=org_id,
        papertrail_token=pt_token or None,
        papertrail_host=pt_host,
        papertrail_port=pt_port,
        audit_webhook_url=audit_webhook or None,
        audit_sheet_id=audit_sheet_id or None,
        audit_service_account_file=audit_sa_file or None,
        audit_service_account_json=audit_sa_json or None,
        web_password=web_password or None,
        web_secret_key=web_secret_key or None,
    )
