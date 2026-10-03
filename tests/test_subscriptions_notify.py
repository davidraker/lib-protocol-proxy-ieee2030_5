"""Subscriptions against the fake server, and the notification receiver over a real TLS loopback socket."""
import asyncio
import datetime
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from protocol_proxy.protocol.ieee2030_5.identity import lfdi_from_cert, sfdi_from_lfdi
from protocol_proxy.protocol.ieee2030_5.models import sep, to_xml
from protocol_proxy.protocol.ieee2030_5.notify import NotifyReceiver

from tests.conftest import make_client
from tests.fake_server import LFDI, FakeSep2Server


def write_self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'volttron-test')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__('ipaddress').ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'dev.pem', tmp_path / 'dev.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


async def test_subscribes_to_controls_defaults_and_fsa(server, clock):
    client = make_client(server, clock, subscribe=True, notify_host='127.0.0.1', notify_port=0)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    subs = server.subscriptions_for()
    assert {s.subscribedResource for s in subs} == {'/derp/1/derc', '/derp/1/dderc', '/edev/1/fsa'}
    assert all(s.notificationURI == client.receiver.url and s.encoding == 0 and s.level == '+S1' for s in subs)
    assert client.receiver.url.startswith('http://127.0.0.1:') and client.receiver.url.endswith('/notify')   # no cert: plain HTTP
    await client.close()
    assert server.subscriptions_for() == []           # deleted on close


async def test_subscribes_through_the_conventional_path_when_the_link_is_missing(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock, advertise_sub_link=False)
    server.add_program()
    client = make_client(server, clock, subscribe=True, notify_host='127.0.0.1', notify_port=0)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.session.subscription_list_href() == '/edev/1/sub' and len(server.subscriptions_for()) == 3
    await client.close()


async def test_falls_back_to_polling_when_the_server_refuses(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock, subscriptions_supported=False)
    server.add_program()
    # The EndDevice still advertises a SubscriptionList, but POSTs to it are refused.
    server.resources['/edev/1'].SubscriptionListLink = sep.SubscriptionListLink(href='/edev/1/sub', all=0)
    client = make_client(server, clock, subscribe=True, notify_host='127.0.0.1', notify_port=0)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.subscriptions.supported is False and client.subscriptions.subscriptions == {}
    assert client.engine._task is not None and not client.engine._task.done()     # polling continues
    await client.close()


async def test_notification_triggers_a_refresh_and_a_push(server, clock, pushed):
    client = make_client(server, clock, subscribe=True, notify_host='127.0.0.1', notify_port=0, pushed=pushed)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    now = client.session.now()
    server.add_control('/derp/1', now - 1, 600, {'opModMaxLimW': 4242}, status=1)
    notification = sep.Notification(subscribedResource='/derp/1/derc', status=0, subscriptionURI='/edev/1/sub/1')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with httpx.AsyncClient(verify=ctx) as http:
        response = await http.post(client.receiver.url, content=to_xml(notification),
                                   headers={'Content-Type': 'application/sep+xml'})
    assert response.status_code == 204
    for _ in range(100):
        await asyncio.sleep(0.02)
        if pushed and pushed[-1].get('der/DERControl/opModMaxLimW') == 4242:
            break
    assert pushed[-1]['der/DERControl/opModMaxLimW'] == 4242
    assert client.receiver.received == 1
    await client.close()


async def test_receiver_http_semantics(tmp_path):
    cert_path, key_path = write_self_signed(tmp_path)
    got = []

    async def handler(notification):
        got.append(notification)

    receiver = NotifyReceiver(handler, host='127.0.0.1', port=0, cert_path=str(cert_path), key_path=str(key_path))
    await receiver.start()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        async with httpx.AsyncClient(verify=ctx) as http:
            body = to_xml(sep.Notification(subscribedResource='/x', status=0, subscriptionURI='/s'))
            assert (await http.post(receiver.url, content=body)).status_code == 204
            assert (await http.get(receiver.url)).status_code == 405
            assert (await http.post(receiver.url.replace('/notify', '/other'), content=body)).status_code == 404
            assert (await http.post(receiver.url, content=b'<nonsense')).status_code == 400
            assert (await http.post(receiver.url, content=b'<Notification xmlns="urn:ieee:std:2030.5:ns"><status>1</status></Notification>')).status_code == 204
    finally:
        await receiver.stop()
    assert len(got) == 2 and got[0].subscribedResource == '/x' and got[1].status == 1
    # The same certificate yields the identifiers the server will see.
    lfdi = lfdi_from_cert(cert_path)
    assert len(lfdi) == 40 and str(sfdi_from_lfdi(lfdi)).isdigit()


async def test_client_derives_lfdi_from_certificate(tmp_path, clock):
    cert_path, key_path = write_self_signed(tmp_path)
    lfdi = lfdi_from_cert(cert_path)
    server = FakeSep2Server(existing_lfdi=lfdi, clock=clock)
    server.add_program()
    client = make_client(server, clock, lfdi=None, cert_path=str(cert_path), key_path=str(key_path))
    assert client.lfdi == lfdi
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.session.end_device.href == '/edev/1' and not client.session.registered_now
    await client.close()


def test_classify_notifications(server, clock):
    client = make_client(server, clock)
    from protocol_proxy.protocol.ieee2030_5.subscriptions import SubscriptionManager
    client.session.end_device = server.resources['/edev/1']
    manager = SubscriptionManager(client.session, 'https://h:1/notify')
    manager.subscriptions['/derp/1/derc'] = '/edev/1/sub/1'
    assert manager.classify(sep.Notification(subscribedResource='/derp/1/derc', status=0)) == ('refresh', '/derp/1/derc')
    assert manager.classify(sep.Notification(subscribedResource='/edev/1/fsa', status=0)) == ('reload', '/edev/1/fsa')
    assert manager.classify(sep.Notification(subscribedResource='/derp/1/derc', status=4)) == ('reload', '/derp/1/derc')
    assert manager.classify(sep.Notification(subscribedResource='/derp/1/derc', status=1)) == ('resubscribe', '/derp/1/derc')
    assert manager.subscriptions == {}
