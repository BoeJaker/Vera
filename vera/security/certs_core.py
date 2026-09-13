"""One certificate list: the rules behind certs.list.

Certificates on the estate came from four places no screen put together:
  the services themselves   the certificate each HTTPS endpoint actually presents
  FreeIPA (Dogtag)          certificates the directory's CA issued
  step-ca                   certificates Vera issued and recorded in OpenBao
  netctl                    the Let's Encrypt wildcard for the DuckDNS name
These rules name each issuer, work out the days left, keep only the newest
FreeIPA certificate per name, and say in plain language what has expired, is
about to, or is missing.

Pure rules, no app imports (tests/test_certs_core.py).
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

SOON_D = 14
NOTICE_D = 30
_DATE_FORMATS = ("%a %b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y %Z", "%Y-%m-%dT%H:%M:%SZ",
                 "%Y%m%d%H%M%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
_RANK = {"error": 0, "warn": 1, "info": 2}


def parse_when(value: Any) -> Optional[float]:
    """Epoch seconds (UTC) from the date formats FreeIPA, openssl and ISO use."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"\s+", " ", str(value).strip())
    for fmt in _DATE_FORMATS:
        try:
            return float(calendar.timegm(time.strptime(s, fmt)))
        except ValueError:
            continue
    return None


def issuer_kind(issuer: str, subject: str = "", self_signed: bool = False) -> str:
    i = (issuer or "").lower()
    if "let's encrypt" in i or "lets encrypt" in i:
        return "Let's Encrypt (staging)" if "staging" in i else "Let's Encrypt"
    if "cn=certificate authority" in i:
        return "FreeIPA"
    if "smallstep" in i or "step-ca" in i:
        return "step-ca"
    if "proxmox" in i:
        return "Proxmox"
    if self_signed or (issuer and issuer == subject):
        return "self-signed"
    return "other"


def days_left(not_after: Optional[float], now: float) -> Optional[int]:
    return None if not_after is None else int((not_after - now) // 86400)


def state_of(days: Optional[int], revoked: bool = False) -> str:
    if revoked:
        return "revoked"
    if days is None:
        return "unknown"
    if days < 0:
        return "expired"
    if days <= SOON_D:
        return "expiring"
    if days <= NOTICE_D:
        return "renew soon"
    return "ok"


def _cn(subject: str) -> str:
    m = re.search(r"CN=([^,]+)", subject or "")
    return m.group(1).strip() if m else (subject or "")


def endpoint_row(probe: Mapping[str, Any], now: float) -> Dict[str, Any]:
    where = f"{probe.get('host')}:{probe.get('port')}"
    if probe.get("error"):
        return {"source": "service", "name": probe.get("name") or where, "where": where, "names": [],
                "issuer": "", "issuer_kind": "", "not_after": None, "days_left": None,
                "state": "unreachable", "detail": str(probe["error"])[:160]}
    days = days_left(probe.get("not_after"), now)
    return {"source": "service", "name": probe.get("name") or where, "where": where,
            "names": list(probe.get("names") or []), "issuer": probe.get("issuer", ""),
            "issuer_kind": issuer_kind(probe.get("issuer", ""), probe.get("subject", ""),
                                       bool(probe.get("self_signed"))),
            "not_after": probe.get("not_after"), "days_left": days, "state": state_of(days)}


def freeipa_rows(certs: Iterable[Mapping[str, Any]], now: float) -> List[Dict[str, Any]]:
    """FreeIPA keeps every certificate it ever issued; only the newest unrevoked one
    per name is current, the rest are marked superseded."""
    rows = []
    for c in certs or []:
        subject = str(c.get("subject") or "")
        not_after = parse_when(c.get("valid_not_after"))
        revoked = bool(c.get("revoked")) or str(c.get("status") or "").upper() == "REVOKED"
        days = days_left(not_after, now)
        rows.append({"source": "freeipa", "name": _cn(subject), "where": "issued by FreeIPA",
                     "serial": str(c.get("serial_number") or ""), "names": [_cn(subject)] if subject else [],
                     "issuer": str(c.get("issuer") or ""), "issuer_kind": "FreeIPA", "not_after": not_after,
                     "days_left": days, "state": state_of(days, revoked)})
    newest: Dict[str, float] = {}
    for r in rows:
        if r["state"] != "revoked" and r["not_after"] is not None:
            newest[r["name"]] = max(newest.get(r["name"], r["not_after"]), r["not_after"])
    for r in rows:
        if r["state"] not in ("revoked",) and r["name"] in newest and r["not_after"] is not None \
                and r["not_after"] < newest[r["name"]]:
            r["state"] = "superseded"
    return rows


def stepca_rows(certs: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [{"source": "step-ca", "name": c.get("fqdn") or "", "where": "recorded by Vera",
             "names": [c.get("fqdn")] if c.get("fqdn") else [], "issuer": "", "issuer_kind": "step-ca",
             "not_after": None, "days_left": None, "state": "recorded", "issued_at": c.get("issued_at")}
            for c in certs or []]


def letsencrypt_row(status: Optional[Mapping[str, Any]], now: float) -> Optional[Dict[str, Any]]:
    if not status:
        return None
    name = status.get("domain") or "Let's Encrypt wildcard"
    w = status.get("wildcard")
    if not w:
        return {"source": "netctl", "name": name, "where": "netctl", "names": [], "issuer": "",
                "issuer_kind": "Let's Encrypt", "not_after": None, "days_left": None, "state": "not issued"}
    days = w.get("days_left")
    return {"source": "netctl", "name": name, "where": "netctl", "names": list(w.get("names") or []),
            "issuer": w.get("issuer", ""),
            "issuer_kind": "Let's Encrypt" if w.get("trusted") else "Let's Encrypt (staging)",
            "not_after": parse_when(w.get("not_after")), "days_left": days, "state": state_of(days)}


def _finding(severity: str, subject: str, message: str, detail: str = "") -> Dict[str, str]:
    return {"severity": severity, "subject": subject, "message": message, "detail": detail}


def summarize(probes: Iterable[Mapping[str, Any]], freeipa: Iterable[Mapping[str, Any]],
              stepca: Iterable[Mapping[str, Any]], letsencrypt: Optional[Mapping[str, Any]],
              errors: Mapping[str, str], now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    rows = [endpoint_row(p, now) for p in probes or []] + freeipa_rows(freeipa, now) + stepca_rows(stepca)
    le = letsencrypt_row(letsencrypt, now)
    if le:
        rows.append(le)
    findings: List[Dict[str, str]] = []
    for r in rows:
        label = f"{r['name']} ({r['where']})" if r["source"] == "service" else r["name"]
        if r["state"] == "expired":
            findings.append(_finding("error", r["name"], f"The certificate for {label} expired "
                                                         f"{-r['days_left']} day(s) ago."))
        elif r["state"] == "expiring":
            findings.append(_finding("warn", r["name"], f"The certificate for {label} expires in "
                                                        f"{r['days_left']} day(s)."))
        elif r["state"] == "renew soon":
            findings.append(_finding("info", r["name"], f"The certificate for {label} expires in "
                                                        f"{r['days_left']} days; renew it this month."))
    unreachable = [r for r in rows if r["state"] == "unreachable"]
    if unreachable:
        findings.append(_finding("info", "services", f"Could not read the certificate from {len(unreachable)} "
                                 f"service(s): " + ", ".join(f"{r['name']} ({r['where']})" for r in unreachable),
                                 "; ".join(r.get("detail", "") for r in unreachable)[:240]))
    selfsigned = [r for r in rows if r["source"] == "service" and r["issuer_kind"] == "self-signed"]
    if selfsigned:
        findings.append(_finding("info", "services", f"{len(selfsigned)} service(s) present a self-signed "
                                 "certificate, so browsers warn unless it is trusted: "
                                 + ", ".join(r["name"] for r in selfsigned)))
    if le and le["state"] == "not issued":
        findings.append(_finding("info", "netctl", f"No Let's Encrypt certificate has been issued for "
                                                   f"{le['name']} yet."))
    for source, err in (errors or {}).items():
        if err:
            findings.append(_finding("warn", source, f"Could not read certificates from {source}.", str(err)[:240]))
    findings.sort(key=lambda f: _RANK[f["severity"]])
    current = [r for r in rows if r["days_left"] is not None and r["state"] not in ("superseded", "revoked")]
    soonest = min(current, key=lambda r: r["days_left"]) if current else None
    rows.sort(key=lambda r: (r["days_left"] is None, r["days_left"] if r["days_left"] is not None else 0, r["name"]))
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {"certs": rows, "counts": counts, "findings": findings,
            "soonest": {"name": soonest["name"], "where": soonest["where"], "days_left": soonest["days_left"]}
            if soonest else None}
