"""Email campaign (newsletter) builder over GHL's internal API (added 2026-10-19, TEG fork).

Reverse-engineered from the email-home-prod / email-builder-prod apps:
  POST   /emails/schedule                         create draft  -> {id}
  POST   /emails/schedule/template-data           save HTML (code editor) -> {previewUrl}
  GET    /emails/schedule/template-data/{loc}/{id} read saved HTML
  PATCH  /emails/schedule/{loc}/{id}              name / previewText / subject / from
  GET    /emails/schedule/{loc}/{id}              campaign record (status, meta)
  GET    /emails/schedule?locationId=..           list
  DELETE /emails/schedule/{loc}/{id}              delete
  POST   /emails/schedule/start                   SEND NOW or SCHEDULE (client-visible!)
  PUT    /emails/schedule/action/cancel/{loc}/{id} cancel a scheduled send
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from cli_anything.gohighlevel.utils import ghl_session
from cli_anything.gohighlevel.utils.ghl_internal_client import InternalGHLClient

_DND = os.path.join(os.path.dirname(__file__), "newsletter_dnd_default.json")


class NewsletterError(RuntimeError):
    pass


def _ok(res: Optional[dict], what: str) -> dict:
    if res is None:
        raise NewsletterError(f"{what}: unauthorized (session refresh failed?)")
    if isinstance(res, dict) and res.get("_error"):
        raise NewsletterError(f"{what}: HTTP {res.get('code')} {res.get('message')}")
    return res


class Newsletters:
    def __init__(self, client: InternalGHLClient):
        self.c = client
        self.loc = client.location_id
        sess = ghl_session.load() or {}
        self.user_id = sess.get("user_id") or ""
        self._user_name: Optional[str] = None

    @property
    def user_name(self) -> str:
        if self._user_name is None:
            u = self.c.request("GET", f"/users/{self.user_id}") or {}
            self._user_name = u.get("name") or " ".join(
                x for x in (u.get("firstName"), u.get("lastName")) if x) or "CLI"
        return self._user_name

    # ── read ──────────────────────────────────────────────────────────────
    def list(self, limit: int = 50, offset: int = 0, status: str = "all") -> dict:
        return _ok(self.c.request(
            "GET", f"/emails/schedule?locationId={self.loc}&limit={limit}&offset={offset}&emailStatus={status}"
        ), "list")

    def get(self, cid: str) -> dict:
        return _ok(self.c.request("GET", f"/emails/schedule/{self.loc}/{cid}"), "get")

    def get_html(self, cid: str) -> dict:
        return _ok(self.c.request("GET", f"/emails/schedule/template-data/{self.loc}/{cid}"), "get html")

    # ── write (drafts) ────────────────────────────────────────────────────
    def create(self, name: str, timezone: str = "America/Winnipeg", folder_id: str = "") -> str:
        res = _ok(self.c.request("POST", "/emails/schedule", {
            "locationId": self.loc, "parentId": folder_id, "byUserName": self.user_name,
            "byUserId": self.user_id, "timeZone": timezone, "templateId": "",
            "templateType": "html", "isPlainText": False, "name": name, "language": "en-US",
        }), "create")
        return res["id"]

    def save_html(self, cid: str, html: str, preview_text: str = "") -> dict:
        with open(_DND, encoding="utf-8") as f:
            dnd = json.load(f)
        return _ok(self.c.request("POST", "/emails/schedule/template-data", {
            "locationId": self.loc, "updatedBy": self.user_id, "dnd": dnd, "html": html,
            "editorType": "html", "previewText": preview_text, "isAutoSave": False,
            "isNewVersion": True, "campaignId": cid,
        }), "save html")

    def update_meta(self, cid: str, *, name: Optional[str] = None, subject: Optional[str] = None,
                    from_name: Optional[str] = None, from_email: Optional[str] = None,
                    reply_to: Optional[str] = None, preview_text: Optional[str] = None) -> dict:
        cur = self.get(cid)
        meta = cur.get("meta") or {}
        bulk = dict(meta.get("bulkReqInfo") or {})
        specs = dict(bulk.get("opSpecs") or {})
        if subject is not None:
            specs["subject"] = subject
        if from_name is not None or from_email is not None:
            old = specs.get("from", "")
            old_name, _, old_addr = old.partition("<")
            fn = from_name if from_name is not None else old_name.strip()
            fe = from_email if from_email is not None else old_addr.rstrip(">").strip()
            specs["from"] = f"{fn} <{fe}>"
        if reply_to is not None:
            specs["replyToAddress"] = reply_to
        bulk["opSpecs"] = specs
        params: dict[str, Any] = {
            "locationId": self.loc, "byUserId": self.user_id,
            "name": name if name is not None else cur.get("name"),
            "previewText": preview_text if preview_text is not None else meta.get("previewText", ""),
            "bulkReqInfo": bulk,
        }
        return _ok(self.c.request("PATCH", f"/emails/schedule/{self.loc}/{cid}", params), "update")

    def delete(self, cid: str) -> dict:
        return _ok(self.c.request("DELETE", f"/emails/schedule/{self.loc}/{cid}"), "delete")

    # ── send / schedule (client-visible) ──────────────────────────────────
    def build_start_payload(self, cid: str, *, tags: list[str] | None = None,
                            contact_ids: list[str] | None = None,
                            all_contacts: bool = False, at: str | None = None,
                            timezone: str = "America/Winnipeg", track_clicks: bool = True) -> dict:
        """Mirror of email-home's w3(); `at` = 'YYYY-MM-DD hh:mm AM' (None = send now)."""
        cur = self.get(cid)
        meta = cur.get("meta") or {}
        specs = (meta.get("bulkReqInfo") or {}).get("opSpecs") or {}
        subject, sender = specs.get("subject", ""), specs.get("from", "").strip()
        if not subject:
            raise NewsletterError("no subject set — use `newsletter update --subject`")
        if "@" not in sender:
            raise NewsletterError("no from address set — use `newsletter update --from-email`")
        picked = sum(bool(x) for x in (tags, contact_ids, all_contacts))
        if picked != 1:
            raise NewsletterError("choose exactly one audience: --tag, --contact or --all-contacts")

        body: dict[str, Any] = {
            "locationId": self.loc, "byUserName": self.user_name, "byUserId": self.user_id,
            "sendDays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "timeZone": timezone, "type": "normal", "campaignId": cid,
            "previewText": meta.get("previewText", ""), "hasTrackingLinks": track_clicks,
            "bulkScheduleType": "SCHEDULED" if at else "NOW",
            "hasUtmTracking": False, "hasTags": False, "enableResendToUnopened": False,
            "resendInfo": {"resendDuration": {"days": 0, "hours": 12, "minutes": 0, "seconds": 0},
                           "resendSubject": ""},
            "selectedDomain": "", "smartSendType": "",
        }
        freeze = True
        if contact_ids:
            body["recordIds"] = contact_ids
        elif tags:
            body["filters"] = {"filters": [{"field": "tags", "operator": "eq", "value": tags}], "assigned_to": ""}
        else:
            body["filters"] = {"filters": [], "assigned_to": ""}
        bulk: dict[str, Any] = {
            "freezeList": freeze, "description": subject, "opSource": "email-builder",
            "opSpecs": {"opType": "bulk-email", "attachments": [], "subject": subject, "from": sender,
                        "assignedTo": "", "useNewApi": True,
                        "replyToAddress": specs.get("replyToAddress", "")},
        }
        if at:
            sched = {"scheduleStart": at, "scheduleTimeZone": timezone}
            if "filters" in body:
                bulk["sprintSchedule"] = {"processAllAtSchedule": True, **sched}
            else:
                bulk["recordListSchedule"] = sched
        body["bulkReqInfo"] = bulk
        return body

    def start(self, payload: dict) -> dict:
        # The app cancels any existing (or half-created) schedule before (re)starting.
        cid = payload["campaignId"]
        if self.get(cid).get("status") not in ("draft", None):
            self.cancel(cid)
        return _ok(self.c.request("POST", "/emails/schedule/start", payload), "start")

    def cancel(self, cid: str) -> dict:
        return _ok(self.c.request("PUT", f"/emails/schedule/action/cancel/{self.loc}/{cid}"), "cancel")
