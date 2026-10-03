"""Subscriptions to the server's DER control resources, and what to do with the notifications they produce."""
from __future__ import annotations

import logging

from .models import convert, sep
from .session import DeviceSession
from .transport import Sep2HttpError

_log = logging.getLogger(__name__)

ENCODING_XML = 0
LEVEL_S1 = '+S1'
#: Notification.status values.
NOTIFY_DEFAULT, NOTIFY_SUBSCRIPTION_DELETED, NOTIFY_RESOURCE_MOVED, NOTIFY_RESOURCE_DEFINITION_CHANGED, NOTIFY_RESOURCE_DELETED = 0, 1, 2, 3, 4


class SubscriptionManager:
    def __init__(self, session: DeviceSession, notification_uri: str | None, limit: int = 50):
        self.session = session
        self.http = session.http
        self.notification_uri = notification_uri
        self.limit = limit
        self.subscriptions: dict[str, str] = {}        # subscribed resource href -> subscription href
        self.supported: bool | None = None

    def targets(self) -> list[str]:
        """The hrefs worth subscribing to: each program's DERControlList and DefaultDERControl, and the FSA list."""
        hrefs: list[str] = []
        for info in self.session.programs:
            program = info.program
            for link in (program.DERControlListLink, program.DefaultDERControlLink):
                if link and link.href and link.href not in hrefs:
                    hrefs.append(link.href)
        if self.session.end_device and self.session.end_device.FunctionSetAssignmentsListLink:
            fsa = self.session.end_device.FunctionSetAssignmentsListLink.href
            if fsa and fsa not in hrefs:
                hrefs.append(fsa)
        return hrefs

    async def subscribe_all(self) -> int:
        """Subscribe to every target not yet subscribed. Returns how many subscriptions are live."""
        list_href = self.session.subscription_list_href()
        if not self.notification_uri or not list_href:
            if self.notification_uri and self.supported is None:
                _log.info('The EndDevice has no SubscriptionList; staying on polling.')
            self.supported = False
            return 0
        for href in self.targets():
            if href in self.subscriptions:
                continue
            subscription = sep.Subscription(subscribedResource=href, encoding=ENCODING_XML, level=LEVEL_S1,
                                            limit=self.limit, notificationURI=self.notification_uri)
            try:
                location = await self.http.post(list_href, subscription)
            except Sep2HttpError as e:
                if e.status is not None and 400 <= e.status < 500:
                    if self.supported is not False:
                        _log.info(f'The server does not accept subscriptions ({e}); staying on polling.')
                    self.supported = False
                    return len(self.subscriptions)
                _log.warning(f'Subscribing to {href} failed: {e}')
                continue
            self.subscriptions[href] = location or ''
            self.supported = True
        return len(self.subscriptions)

    async def unsubscribe_all(self):
        for href, sub_href in list(self.subscriptions.items()):
            if sub_href:
                try:
                    await self.http.delete(sub_href)
                except Sep2HttpError as e:
                    _log.debug(f'Deleting subscription {sub_href} failed: {e}')
            self.subscriptions.pop(href, None)

    def classify(self, notification: sep.Notification) -> tuple[str, str | None]:
        """``(action, resource href)``: ``refresh`` the controls, ``resubscribe``, ``reload`` the program list."""
        resource = notification.subscribedResource
        status = notification.status if notification.status is not None else NOTIFY_DEFAULT
        if status == NOTIFY_SUBSCRIPTION_DELETED:
            self.subscriptions.pop(resource, None)
            return 'resubscribe', resource
        fsa = self.session.end_device.FunctionSetAssignmentsListLink.href \
            if self.session.end_device and self.session.end_device.FunctionSetAssignmentsListLink else None
        if resource == fsa or status in (NOTIFY_RESOURCE_MOVED, NOTIFY_RESOURCE_DEFINITION_CHANGED, NOTIFY_RESOURCE_DELETED):
            return 'reload', resource
        return 'refresh', resource

    @staticmethod
    def describe(notification: sep.Notification) -> dict:
        return {'resource': notification.subscribedResource, 'status': notification.status,
                'subscription': notification.subscriptionURI, 'new_resource': notification.newResourceURI,
                'payload': convert.flatten(notification.Resource) if notification.Resource is not None else None}
