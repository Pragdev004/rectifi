"""The Phone page: how to reach Rectifi from a phone, the SMS gateway, paired phones and the "Needs review" inbox.

Only POST /api/phone/devices ever returns a device token (once, with Cache-Control: no-store). Nothing here logs a token."""

from __future__ import annotations

import json

from starlette.responses import JSONResponse
from starlette.routing import Route

from ... import config, forwarder_config, sms_gateway, tailscale, tools
from ..inputs import api, path_int, text_field
from .. import wire

SETUP_UNAVAILABLE = ("The setup file needs the address your phone will post to, and there is none yet. The gateway listens on this "
                     "laptop only, so the phone reaches it through Tailscale: install Tailscale on the laptop and the phone (same "
                     "account), run `python scripts/dev.py tailscale`, then reload this page.")


def tailscale_info() -> dict:
    """What Tailscale says about this laptop right now: installed? running? its MagicDNS name? which https ports are published?
    Two quick commands, nothing cached, so the page is always current. Without Tailscale nothing is run at all."""
    cli = tailscale.find_cli()
    name = tailscale.dns_name(tailscale.read_status(cli)) if cli else None
    ports = tailscale.read_served_ports(cli) if name else set()
    return {"installed": cli is not None, "name": name, "app_published": tailscale.HTTPS_APP_PORT in ports,
            "gateway_published": tailscale.HTTPS_GATEWAY_PORT in ports}


def gateway_addresses(info: dict, port: int) -> dict:
    """The address a phone posts to: the Tailscale HTTPS one, else (only when the host was opened on purpose) a LAN one."""
    open_lan = config.sms_host_is_open()
    lan = sms_gateway.private_ipv4s() if open_lan else []
    secure = tailscale.gateway_url(info["name"]) if info["name"] else None
    url = secure or (f"http://{lan[0]['ip']}:{port}/sms" if lan else None)
    health = f"{secure.rsplit('/sms', 1)[0]}/health" if secure else (f"http://{lan[0]['ip']}:{port}/health" if lan else None)
    return {"url": url, "health": health, "lan": lan, "open": open_lan}


@api()
def status(request):
    info = tailscale_info()
    port = config.sms_port()
    where = gateway_addresses(info, port)
    return {
        "tailscale": info,
        "app_url": tailscale.app_url(info["name"]) if info["name"] else None,
        "gateway": {"up": sms_gateway.gateway_reachable(port), "port": port, "host": config.sms_host(), "open": where["open"]},
        "gateway_url": where["url"], "health_url": where["health"], "lan_addresses": where["lan"],
        "tailscale_login": request.headers.get("tailscale-user-login"),
    }


@api()
def devices(request):
    return [{"id": d["id"], "name": d["name"], "created_at": d["created_at"], "last_seen_at": d["last_seen_at"],
             "revoked_at": d["revoked_at"]} for d in tools.list_devices()]


@api(body=True)
def pair(request, data):
    name = text_field(data, "name", required=False, max_len=tools.DEVICE_NAME_MAX * 2)
    device = tools.create_device(name or "")
    token = device["token"]
    info = tailscale_info()
    where = gateway_addresses(info, config.sms_port())
    setup_file = reason = None
    if where["url"]:
        content = forwarder_config.build_forwarder_rules(where["url"], token, device_id=device["id"], text_filter=forwarder_config.TEXT_REGEX)
        setup_file = {"filename": forwarder_config.FILE_NAME, "content": content}
    else:
        reason = SETUP_UNAVAILABLE
    body = {
        "device": {"id": device["id"], "name": device["name"], "created_at": device["created_at"]},
        "token": token,
        "setup_file": setup_file, "setup_unavailable_reason": reason,
        "manual": {
            "webhook_url": where["url"], "headers_json": json.dumps({"Authorization": f"Bearer {token}"}),
            "template": forwarder_config.JSON_TEMPLATE, "sender_regex": forwarder_config.SENDER_REGEX,
            "text_regex": forwarder_config.TEXT_REGEX,
        },
    }
    return JSONResponse(wire.to_wire(body), headers={"Cache-Control": "no-store"})


@api()
def revoke(request):
    return {"revoked": tools.revoke_device(path_int(request, "device_id"))}


@api(body=True)
def test_message(request, data):
    token = text_field(data, "token", max_len=200)
    status_code, said = sms_gateway.send_test_sms(token, config.sms_port())
    return {"status": status_code, "said": said}


@api()
def inbox(request):
    return [{"id": i["id"], "sender": i["sender"], "raw_text": i["raw_text"], "sent_at": i["sent_at"], "received_at": i["received_at"],
             "reason": i["reason"], "device": i["device"]} for i in tools.list_inbox("pending")]


@api()
def dismiss(request):
    item_id = path_int(request, "item_id")
    return {"dismissed": tools.dismiss_inbox_item(item_id)}


routes = [
    Route("/phone/status", status, methods=["GET"]),
    Route("/phone/devices", devices, methods=["GET"]),
    Route("/phone/devices", pair, methods=["POST"]),
    Route("/phone/devices/{device_id}/revoke", revoke, methods=["POST"]),
    Route("/phone/test", test_message, methods=["POST"]),
    Route("/phone/inbox", inbox, methods=["GET"]),
    Route("/phone/inbox/{item_id}/dismiss", dismiss, methods=["POST"]),
]

