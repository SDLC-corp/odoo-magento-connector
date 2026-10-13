"""Receiver of the real-time pushes of the SDLC_OdooConnector Magento module (roadmap 3.3).

Unauthenticated route (``auth="none"``): the caller is authenticated by the HMAC signature
of the instance's shared secret, checked in ``magento.instance._magento_webhook_handle``.
The work done here is a few inserts (events, queued jobs); the records are re-fetched from
Magento later by the job runner, so the response is fast and never carries data.
"""
from odoo import SUPERUSER_ID, http
from odoo.http import request

from ..models.magento_webhook import MAX_BODY_BYTES


class MagentoWebhookController(http.Controller):

    @http.route("/magento/webhook/<string:webhook_uuid>", type="http", auth="none",
                methods=["POST"], csrf=False, save_session=False, readonly=False)
    def magento_webhook(self, webhook_uuid, **_kwargs):
        if not request.db:
            return request.make_json_response({"error": "not found"}, status=404)
        httprequest = request.httprequest
        if (httprequest.content_length or 0) > MAX_BODY_BYTES:
            return request.make_json_response({"error": "payload too large"}, status=413)
        # Cached: a serialization failure makes Odoo replay the request on the same body.
        body = httprequest.get_data()
        # Technical user: the superuser; the handler switches to the instance's company.
        env = request.env(user=SUPERUSER_ID)
        status, data = env["magento.instance"]._magento_webhook_handle(
            webhook_uuid, body, httprequest.headers,
        )
        return request.make_json_response(data, status=status)
