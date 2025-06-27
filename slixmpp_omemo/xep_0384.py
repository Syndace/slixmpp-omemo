from abc import ABCMeta, abstractmethod
import asyncio
from copy import copy
import logging
from typing import cast, Any, Awaitable, Dict, FrozenSet, Optional, Set, Tuple, Type, Union
from xml.etree import ElementTree as ET

import omemo
from omemo.session_manager import EncryptionError, SessionManager
from omemo.session_manager import (
    BundleDeletionFailed,
    BundleDownloadFailed,
    BundleNotFound,
    BundleUploadFailed,
    DeviceListDownloadFailed,
    DeviceListUploadFailed,
    MessageNotForUs,
    MessageSendingFailed,
    SenderNotFound,
    UnknownNamespace
)
from omemo.storage import Storage
from omemo.types import DeviceInformation, DeviceList

import oldmemo
import oldmemo.etree
from oldmemo.migrations import LegacyStorage
import twomemo
import twomemo.etree

from xmlschema import XMLSchemaValidationError

from slixmpp.basexmpp import BaseXMPP
from slixmpp.exceptions import IqError
from slixmpp.jid import JID  # pylint: disable=no-name-in-module
from slixmpp.plugins.base import BasePlugin
from slixmpp.plugins.xep_0004 import Form  # type: ignore[attr-defined]
from slixmpp.plugins.xep_0045 import XEP_0045  # type: ignore[attr-defined]
from slixmpp.plugins.xep_0060 import XEP_0060  # type: ignore[attr-defined]
from slixmpp.plugins.xep_0163 import XEP_0163
from slixmpp.roster import RosterNode  # type: ignore[attr-defined]
from slixmpp.stanza import Iq, Message, Presence
from slixmpp.xmlstream import XMLStream

from .base_session_manager import BaseSessionManager, TrustLevel


__all__ = [
    "XEP_0384"
]


TWOMEMO_DEVICE_LIST_NODE = "urn:xmpp:omemo:2:devices"
OLDMEMO_DEVICE_LIST_NODE = "eu.siacs.conversations.axolotl.devicelist"


log = logging.getLogger(__name__)


def _make_options_form(form_type: str, fields: Dict[str, Any]) -> Form:
    """
    Build a form for publish options or manual pubsub node configuration.

    Args:
        form_type: The value of the form type field, either selecting publish-options or node_config.
        fields: The options to set.

    Returns:
        The filled-out form ready to be included in a publish or node configuration call.
    """

    form = Form()
    form["type"] = "submit"
    form.add_field(var="FORM_TYPE", ftype="hidden", value=form_type)

    for key, value in fields.items():
        form.add_field(var=key, value=value)

    return form


async def _publish_item_and_configure_node(
    xep_0060: XEP_0060,
    service: str,
    node: str,
    item: ET.Element,
    item_id: str,
    options: Dict[str, str]
) -> None:
    """
    Publishes an item and makes sure that the node is configured correctly.

    Args:
        xep_0060: The XEP_0060 instance for pubsub interaction.
        service: The pubsub service to publish to.
        node: The pubsub node to publish to.
        item: The item to publish.
        item_id: The item id to assign to the published item.
        options: The configuration required on the target node. The configuration is applied either
            dynamically using publish options or manually using pubsub node configuration.

    Raises:
        Exception: all exceptions raised by :meth:`XEP_0060.publish` and :meth:`XEP_0060.set_node_config` are
            forwarded as-is.
    """

    publish_options_form = _make_options_form("http://jabber.org/protocol/pubsub#publish-options", options)
    node_config_form = _make_options_form("http://jabber.org/protocol/pubsub#node_config", options)

    try:
        await xep_0060.publish(JID(service), node, item_id, item, publish_options_form)
    except IqError as e:
        # There doesn't seem to be a clean way to find the error condition from an IqError yet.
        if e.iq["error"].xml.find("{http://jabber.org/protocol/pubsub#errors}precondition-not-met") is None:
            raise

        # precondition-not-met is raised in case the node already exists with different configuration. Try
        # to manually reconfigure the node as needed.
        await xep_0060.set_node_config(JID(service), node, node_config_form)

        # Attempt to publish the item again. This time, precondition-not-met should not fire.
        await xep_0060.publish(JID(service), node, item_id, item, publish_options_form)


async def _download_bundle(
    xep_0060: XEP_0060,
    namespace: str,
    bare_jid: str,
    device_id: int
) -> omemo.Bundle:
    """
    Implementation of :meth:`~omemo.session_manager.SessionManager._download_bundle`, extracted as standalone
    to make it usable for :func:`~oldmemo.migrations.migrate`. For details, check the docs of
    `_download_bundle`.
    """

    items_iq: Optional[Iq] = None
    try:
        if namespace == twomemo.twomemo.NAMESPACE:
            node = "urn:xmpp:omemo:2:bundles"
            items_iq = await xep_0060.get_items(JID(bare_jid), node, item_ids=[ str(device_id) ])
        if namespace == oldmemo.oldmemo.NAMESPACE:
            node = f"eu.siacs.conversations.axolotl.bundles:{device_id}"
            items_iq = await xep_0060.get_items(JID(bare_jid), node, max_items=1)
    except Exception as e:
        if isinstance(e, IqError):
            if e.condition == "item-not-found":
                raise BundleNotFound(
                    f"Bundle of {bare_jid}: {device_id} not found under namespace {namespace}. The"
                    f" node doesn't exist."
                ) from e

        raise BundleDownloadFailed(
            f"Bundle download failed for {bare_jid}: {device_id} under namespace {namespace}"
        ) from e

    if items_iq is None:
        raise UnknownNamespace(f"Unknown namespace: {namespace}")

    items = items_iq["pubsub"]["items"]

    if len(items) == 0:
        raise BundleNotFound(
            f"Bundle of {bare_jid}: {device_id} not found under namespace {namespace}. The node"
            f" exists but is empty."
        )

    if len(items) > 1:
        raise BundleDownloadFailed(
            f"Bundle download failed for {bare_jid}: {device_id} under namespace {namespace}:"
            f" Unexpected number of items retrieved: {len(items)}."
        )

    bundle_elt = next(iter(items["item"].xml), None)
    if bundle_elt is None:
        raise BundleDownloadFailed(
            f"Bundle download failed for {bare_jid}: {device_id} under namespace {namespace}: Pubsub"
            f" item is empty."
        )

    try:
        if namespace == twomemo.twomemo.NAMESPACE:
            return twomemo.etree.parse_bundle(bundle_elt, bare_jid, device_id)
        if namespace == oldmemo.oldmemo.NAMESPACE:
            return oldmemo.etree.parse_bundle(bundle_elt, bare_jid, device_id)
    except Exception as e:
        raise BundleDownloadFailed(
            f"Bundle parsing failed for {bare_jid}: {device_id} under namespace {namespace}"
        ) from e

    raise UnknownNamespace(f"Unknown namespace: {namespace}")


def _make_session_manager(xmpp: BaseXMPP, xep_0384: "XEP_0384") -> Type[SessionManager]:
    """
    Returns an implementation of `SessionManager` that is tailored for use in the plugin. Pubsub interactions
    are handled via the XEP_0060 plugin, messages are sent via the `BaseXMPP` instance and BTBV & manual trust
    are provided as trust systems.

    Args:
        xmpp: The BaseXMPP object for interaction with Slixmpp/XMPP.
        xep_0384: The plugin instance.

    Returns:
        The session manager implementation type, ready to be instantiated.
    """

    our_bare_jid: str = xmpp.boundjid.bare
    xep_0060: XEP_0060 = xmpp["xep_0060"]

    class SessionManagerImpl(BaseSessionManager):
        @staticmethod
        async def _upload_bundle(bundle: omemo.Bundle) -> None:
            if isinstance(bundle, twomemo.twomemo.BundleImpl):
                node = "urn:xmpp:omemo:2:bundles"
                item = twomemo.etree.serialize_bundle(bundle)

                try:
                    await _publish_item_and_configure_node(
                        xep_0060,
                        our_bare_jid,
                        node,
                        item,
                        item_id=str(bundle.device_id),
                        options={
                            "pubsub#access_model": "open",
                            "pubsub#persist_items": "true",
                            "pubsub#max_items": "max"
                        }
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    # Try again without MAX_ITEMS set, which is not strictly necessary.
                    try:
                        await _publish_item_and_configure_node(
                            xep_0060,
                            our_bare_jid,
                            node,
                            item,
                            item_id=str(bundle.device_id),
                            options={
                                "pubsub#access_model": "open",
                                "pubsub#persist_items": "true"
                            }
                        )
                    except Exception as e:
                        raise BundleUploadFailed(f"Bundle upload failed: {bundle}") from e

                return

            if isinstance(bundle, oldmemo.oldmemo.BundleImpl):
                node = f"eu.siacs.conversations.axolotl.bundles:{bundle.device_id}"
                item = oldmemo.etree.serialize_bundle(bundle)

                try:
                    await _publish_item_and_configure_node(
                        xep_0060,
                        our_bare_jid,
                        node,
                        item,
                        item_id="current",
                        options={
                            "pubsub#access_model": "open",
                            "pubsub#persist_items": "true",
                            "pubsub#max_items": "1"
                        }
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    # Try again without MAX_ITEMS set, which is not strictly necessary.
                    try:
                        await _publish_item_and_configure_node(
                            xep_0060,
                            our_bare_jid,
                            node,
                            item,
                            item_id="current",
                            options={
                                "pubsub#access_model": "open",
                                "pubsub#persist_items": "true"
                            }
                        )
                    except Exception as e:
                        raise BundleUploadFailed(f"Bundle upload failed: {bundle}") from e

                return

            raise UnknownNamespace(f"Unknown namespace: {bundle.namespace}")

        @staticmethod
        async def _download_bundle(namespace: str, bare_jid: str, device_id: int) -> omemo.Bundle:
            return await _download_bundle(xep_0060, namespace, bare_jid, device_id)

        @staticmethod
        async def _delete_bundle(namespace: str, device_id: int) -> None:
            if namespace == twomemo.twomemo.NAMESPACE:
                node = "urn:xmpp:omemo:2:bundles"

                try:
                    await xep_0060.retract(JID(our_bare_jid), node, [ str(device_id) ], notify=False)
                except Exception as e:
                    if isinstance(e, IqError):
                        if e.condition == "item-not-found":
                            return

                    raise BundleDeletionFailed(
                        f"Bundle deletion failed for {device_id} under namespace {namespace}"
                    ) from e

                return

            if namespace == oldmemo.oldmemo.NAMESPACE:
                node = f"eu.siacs.conversations.axolotl.bundles:{device_id}"

                try:
                    await xep_0060.delete_node(JID(our_bare_jid), node)
                except Exception as e:
                    if isinstance(e, IqError):
                        if e.condition == "item-not-found":
                            return

                    raise BundleDeletionFailed(
                        f"Bundle deletion failed for {device_id} under namespace {namespace}"
                    ) from e

                return

            raise UnknownNamespace(f"Unknown namespace: {namespace}")

        @staticmethod
        async def _upload_device_list(namespace: str, device_list: DeviceList) -> None:
            item: Optional[ET.Element] = None
            node: Optional[str] = None

            if namespace == twomemo.twomemo.NAMESPACE:
                item = twomemo.etree.serialize_device_list(device_list)
                node = TWOMEMO_DEVICE_LIST_NODE

            if namespace == oldmemo.oldmemo.NAMESPACE:
                item = oldmemo.etree.serialize_device_list(device_list)
                node = OLDMEMO_DEVICE_LIST_NODE

            if item is None or node is None:
                raise UnknownNamespace(f"Unknown namespace: {namespace}")

            try:
                await _publish_item_and_configure_node(
                    xep_0060,
                    our_bare_jid,
                    node,
                    item,
                    item_id="current",
                    options={
                        "pubsub#access_model": "open",
                        "pubsub#persist_items": "true",
                        "pubsub#max_items": "1"
                    }
                )
            except Exception:  # pylint: disable=broad-exception-caught
                try:
                    # Try again without MAX_ITEMS set, which is not strictly necessary.
                    await _publish_item_and_configure_node(
                        xep_0060,
                        our_bare_jid,
                        node,
                        item,
                        item_id="current",
                        options={
                            "pubsub#access_model": "open",
                            "pubsub#persist_items": "true"
                        }
                    )
                except Exception as e:
                    raise DeviceListUploadFailed(
                        f"Device list upload failed for namespace {namespace}"
                    ) from e

        @staticmethod
        async def _download_device_list(namespace: str, bare_jid: str) -> DeviceList:
            node: Optional[str] = None

            if namespace == twomemo.twomemo.NAMESPACE:
                node = TWOMEMO_DEVICE_LIST_NODE
            if namespace == oldmemo.oldmemo.NAMESPACE:
                node = OLDMEMO_DEVICE_LIST_NODE

            if node is None:
                raise UnknownNamespace(f"Unknown namespace: {namespace}")

            try:
                items_iq = await xep_0060.get_items(JID(bare_jid), node, max_items=1)
            except Exception as e:  # pylint: disable=broad-exception-caught
                if isinstance(e, IqError):
                    if e.condition == "item-not-found":
                        return {}

                log.warning(
                    f"Device list download failed for {bare_jid} under namespace {namespace}, trying again"
                    f" without max_items"
                )

                try:
                    items_iq = await xep_0060.get_items(JID(bare_jid), node)
                except Exception as ex:
                    if isinstance(ex, IqError):
                        if ex.condition == "item-not-found":
                            return {}

                    raise DeviceListDownloadFailed(
                        f"Device list download failed for {bare_jid} under namespace {namespace}"
                    ) from ex

            items = items_iq["pubsub"]["items"]

            if len(items) == 0:
                return {}

            if len(items) > 1:
                raise DeviceListDownloadFailed(
                    f"Device list download failed for {bare_jid} under namespace {namespace}: Unexpected"
                    f" number of items retrieved: {len(items)}."
                )

            device_list_elt = next(iter(items["item"].xml), None)
            if device_list_elt is None:
                raise DeviceListDownloadFailed(
                    f"Device list download failed for {bare_jid} under namespace {namespace}: Pubsub item is"
                    f" empty."
                )

            try:
                if namespace == twomemo.twomemo.NAMESPACE:
                    return twomemo.etree.parse_device_list(device_list_elt)
                if namespace == oldmemo.oldmemo.NAMESPACE:
                    return oldmemo.etree.parse_device_list(device_list_elt)
            except XMLSchemaValidationError as e:
                log.warning(
                    f"Malformed device list for {bare_jid} under namespace {namespace}, treating as empty",
                    exc_info=e
                )
                return {}
            except Exception as e:
                raise DeviceListDownloadFailed(
                    f"Device list download failed for {bare_jid} under namespace {namespace}"
                ) from e

            raise UnknownNamespace(f"Unknown namespace: {namespace}")

        @property
        def _btbv_enabled(self) -> bool:
            return xep_0384._btbv_enabled  # pylint: disable=protected-access

        async def _devices_blindly_trusted(
            self,
            blindly_trusted: FrozenSet[DeviceInformation],
            identifier: Optional[str]
        ) -> None:
            return await xep_0384._devices_blindly_trusted(  # pylint: disable=protected-access
                blindly_trusted,
                identifier
            )

        async def _prompt_manual_trust(
            self,
            manually_trusted: FrozenSet[DeviceInformation],
            identifier: Optional[str]
        ) -> None:
            return await xep_0384._prompt_manual_trust(  # pylint: disable=protected-access
                manually_trusted,
                identifier
            )

        @staticmethod
        async def _send_message(message: omemo.Message, bare_jid: str) -> None:
            element: Optional[ET.Element] = None

            if message.namespace == twomemo.twomemo.NAMESPACE:
                element = twomemo.etree.serialize_message(message)
            if message.namespace == oldmemo.oldmemo.NAMESPACE:
                element = oldmemo.etree.serialize_message(message)

            if element is None:
                raise UnknownNamespace(f"Unknown namespace: {message.namespace}")

            msg = xmpp.make_message(mto=JID(bare_jid))
            msg.append(element)
            msg.enable("store")
            try:
                # send() can't actually throw; it simply queues up the message and returns. The try/catch is
                # here in case there's ever a throwing send.
                msg.send()
            except Exception as e:
                raise MessageSendingFailed() from e

    return SessionManagerImpl


async def _prepare(
    xmpp: BaseXMPP,
    xep_0384: "XEP_0384",
    storage: Storage,
    legacy_storage: Optional[LegacyStorage],
    signed_pre_key_rotation_period: int = 7 * 24 * 60 * 60,
    pre_key_refill_threshold: int = 99,
    max_num_per_session_skipped_keys: int = 1000,
    max_num_per_message_skipped_keys: Optional[int] = None
) -> SessionManager:
    """
    Prepare the OMEMO library for use in this plugin.

    Args:
        xmpp: The BaseXMPP object for interaction with Slixmpp/XMPP.
        xep_0384: The plugin instance.
        storage: The storage for all OMEMO-related data.
        legacy_storage: Optional legacy storage to migrate data from.
        signed_pre_key_rotation_period: The rotation period for the signed pre key, in seconds. The rotation
            period is recommended to be between one week (the default) and one month.
        pre_key_refill_threshold: The number of pre keys that triggers a refill to 100. Defaults to 99, which
            means that each pre key gets replaced with a new one right away. The threshold can not be
            configured to lower than 25.
        max_num_per_session_skipped_keys: The maximum number of skipped message keys to keep around per
            session. Once the maximum is reached, old message keys are deleted to make space for newer ones.
        max_num_per_message_skipped_keys: The maximum number of skipped message keys to accept in a single
            message. When set to ``None`` (the default), this parameter defaults to the per-session maximum
            (i.e. the value of the ``max_num_per_session_skipped_keys`` parameter). This parameter may only be
            0 if the per-session maximum is 0, otherwise it must be a number between 1 and the per-session
            maximum.

    Returns:
        The session manager, i.e. the OMEMO library's core interface, initialized for use with Slixmpp.

    Raises:
        Exception: all exceptions raised by :meth:`SessionManager.create` are forwarded as-is.
    """

    if legacy_storage is not None:
        xep_0060: XEP_0060 = xmpp["xep_0060"]

        await oldmemo.migrations.migrate(
            legacy_storage,
            storage,
            # Taking the safe path here by resetting all trust to at most undecided. This is not optimal, but
            # the complexity of making this configurable outweighs the expected use.
            TrustLevel.UNDECIDED.value,
            TrustLevel.UNDECIDED.value,
            TrustLevel.DISTRUSTED.value,
            lambda bare_jid, device_id: cast(Awaitable[oldmemo.oldmemo.BundleImpl], _download_bundle(
                xep_0060,
                oldmemo.oldmemo.NAMESPACE,
                bare_jid,
                device_id
            ))
        )

    session_manager = await _make_session_manager(xmpp, xep_0384).create(
        [
            twomemo.Twomemo(
                storage,
                max_num_per_session_skipped_keys,
                max_num_per_message_skipped_keys
            ),
            oldmemo.Oldmemo(
                storage,
                max_num_per_session_skipped_keys,
                max_num_per_message_skipped_keys
            )
        ],
        storage,
        xmpp.boundjid.bare,
        initial_own_label=None,
        undecided_trust_level_name=TrustLevel.UNDECIDED.value,
        signed_pre_key_rotation_period=signed_pre_key_rotation_period,
        pre_key_refill_threshold=pre_key_refill_threshold
    )

    # This shouldn't hurt here since we're not running on overly constrainted devices.
    # TODO: Consider ensuring data consistency regularly/in response to certain events
    await session_manager.ensure_data_consistency()

    # TODO: Correct entering/leaving of the history synchronization mode isn't terribly important for now,
    # since it only prevents an extremely unlikely race condition of multiple devices choosing the same pre
    # key for new sessions while the device was offline. I don't believe other clients seriously defend
    # against that race condition either. In the long run, it might still be cool to have triggers for when
    # history sync starts and ends (MAM, MUC catch-up, etc.) and to react to those triggers.
    await session_manager.after_history_sync()

    return session_manager


class XEP_0384(BasePlugin, metaclass=ABCMeta):  # pylint: disable=invalid-name
    """
    An implementation of XEP-0384: OMEMO Encryption.

    Supports both the 0.3 version of the protocol (under the eu.siacs.conversations.axolotl namespace, also
    known as legacy OMEMO, oldmemo and siacs OMEMO) and the current 0.8 version (under the omemo:2 namespace,
    also known as newmemo, twomemo and OMEMO 2).

    The plugin does not treat the protocol versions as separate encryption mechanisms, instead it manages all
    versions transparently with no manual intervention required.

    Certain initialization tasks such as a data consistency check are transparently ran in the background when
    the plugin is loaded. The ``omemo_initialized`` event is fired when those initial background tasks are
    done. Waiting for this event can be useful e.g. in automated testing environments to be sure that a test
    client has generated and uploaded its OMEMO data before continuing.

    Tip:
        A lot of essential functionality is accessible via the `SessionManager` instance that is returned by
        :meth:`get_session_manager`. The session manager is the core of the underlying OMEMO library and
        offers functionality such as listing all devices known for an XMPP account, managing trust and
        settings your own device's label. Refer to the library's
        `API Documentation <https://py-omemo.readthedocs.io/omemo/session_manager.html>`__ for details.
    """

    name = "xep_0384"
    description = "OMEMO Encryption"
    dependencies = { "xep_0004", "xep_0030", "xep_0060", "xep_0163", "xep_0280", "xep_0334" }
    default_config = {
        # TODO: Improve fallback text :)
        "fallback_message": "This message is OMEMO encrypted."
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.__session_manager: Optional[SessionManager] = None
        self.__session_manager_task: Optional[asyncio.Task[SessionManager]] = None

        # Mapping from stanza id to plaintext. Since a new id is generated for each outgoing stanza, the
        # protocol version does not need to be stored in addition.
        self.__muc_reflection_cache: Dict[str, bytes] = {}

    def plugin_init(self) -> None:
        xmpp: BaseXMPP = self.xmpp

        xep_0060: XEP_0060 = xmpp["xep_0060"]
        xep_0163: XEP_0163 = xmpp["xep_0163"]

        xep_0060.map_node_event(TWOMEMO_DEVICE_LIST_NODE, "twomemo_device_list")
        xep_0060.map_node_event(OLDMEMO_DEVICE_LIST_NODE, "oldmemo_device_list")

        xmpp.add_event_handler("twomemo_device_list_publish", self._on_device_list_update)
        xmpp.add_event_handler("oldmemo_device_list_publish", self._on_device_list_update)

        xmpp.add_event_handler("changed_subscription", self._on_subscription_changed)

        xep_0163.add_interest(TWOMEMO_DEVICE_LIST_NODE)
        xep_0163.add_interest(OLDMEMO_DEVICE_LIST_NODE)

    def plugin_end(self) -> None:
        xmpp: BaseXMPP = self.xmpp

        xep_0163: XEP_0163 = xmpp["xep_0163"]

        xmpp.del_event_handler("twomemo_device_list_publish", self._on_device_list_update)
        xmpp.del_event_handler("oldmemo_device_list_publish", self._on_device_list_update)

        xep_0163.remove_interest(TWOMEMO_DEVICE_LIST_NODE)
        xep_0163.remove_interest(OLDMEMO_DEVICE_LIST_NODE)

        if self.__session_manager is not None:
            asyncio.create_task(self.__session_manager.shutdown())

        self.__session_manager = None
        if self.__session_manager_task is not None:
            self.__session_manager_task.cancel()
            self.__session_manager_task = None

    def session_bind(self, jid: JID) -> None:
        # Trigger async creation of the session manager
        asyncio.create_task(self.get_session_manager())

    @property
    @abstractmethod
    def storage(self) -> Storage:
        """
        Returns:
            The storage implementation to use to store OMEMO-related data.
        """

    @property
    def legacy_storage(self) -> Optional[LegacyStorage]:
        """
        This property can be overridden to have the plugin perform migration from a legacy storage backend
        (python-omemo versions older than v1.0.0) to the new storage backend returned by :meth:`storage`.
        This migration is fully automatic and idempotent.

        Returns:
            A legacy storage backend implementation to migrate data from, otherwise `None`.
        """

        return None

    @property
    @abstractmethod
    def _btbv_enabled(self) -> bool:
        """
        Returns:
            Whether BTBV is enabled.
        """

    async def _devices_blindly_trusted(
        self,
        blindly_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str]
    ) -> None:
        """
        Get notified about newly blindly trusted devices. This method is called automatically by  whenever at
        least one device was blindly trusted. You can use this method for example to notify the user about the
        automated change in trust.

        Does nothing by default.

        Args:
            blindly_trusted: A set of devices that were blindly trusted.
            identifier: A piece of application-specific information that callers can pass to
                :meth:`encrypt_message`, which is then forwarded here unaltered. This can be used, for
                example, by instant messaging clients, to identify the chat tab which triggered the call to
                :meth:`encrypt_message` and subsequently this call to :meth:`_devices_blindly_trusted`.
        """

    @abstractmethod
    async def _prompt_manual_trust(
        self,
        manually_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str]
    ) -> None:
        """
        Prompt manual trust decision on a set of undecided identity keys. The trust decisions are expected to
        be persisted by calling :meth:`~omemo.session_manager.SessionManager.set_trust`.

        Args:
            manually_trusted: A set of devices whose trust has to be manually decided by the user.
            identifier: A piece of application-specific information that callers can pass to
                :meth:`encrypt_message`, which is then forwarded here unaltered. This can be used, for
                example, by instant messaging clients, to identify the chat tab which triggered the call to
                :meth:`encrypt_message` and subsequently this call to :meth:`_prompt_manual_trust`.

        Raises:
            TrustDecisionFailed: if for any reason the trust decision failed/could not be completed. Feel free
                to raise a subclass instead.

        Note:
            This is called when the encryption needs to know whether it is allowed to encrypt for these
            devices or not. When this method returns, all previously undecided trust levels should have been
            replaced by calling :meth:`~omemo.session_manager.SessionManager.set_trust` with a different trust
            level. If they are not replaced or still evaluate to the undecided trust level after the call, the
            encryption will fail with an exception. See :meth:`~omemo.session_manager.SessionManager.encrypt`
            for details.
        """

    async def get_session_manager(self) -> SessionManager:
        """
        Access the session manager, which is the main interface to the underlying OMEMO library. A lot of
        useful functionality is available on the session manager, refer to the library's
        `API Documentation <https://py-omemo.readthedocs.io/omemo/session_manager.html>`__ for details.

        Returns:
            The session manager instance that is internally used by this plugin.
        """

        # If the session manager is available, return it
        if self.__session_manager is not None:
            return self.__session_manager

        # If the session manager is neither available nor currently being built, build it in a way that other
        # tasks can await the build task
        if self.__session_manager_task is None:
            self.__session_manager_task = asyncio.create_task(_prepare(
                self.xmpp,
                self,
                self.storage,
                self.legacy_storage
            ))
            session_manager = await self.__session_manager_task
            self.__session_manager = session_manager
            self.__session_manager_task = None
            self.xmpp.event("omemo_initialized")
            return session_manager

        # If the session manager is currently being built, wait for it to be done
        return await self.__session_manager_task

    async def _on_device_list_update(self, msg: Message) -> None:
        """
        Callback to handle PEP updates to the device list node of either OMEMO protocol version.

        Args:
            msg: The stanza containing the PEP update event.
        """

        items = msg["pubsub_event"]["items"]

        if len(items) == 0:
            log.debug("Ignoring empty device list update.")
            return

        if len(items) > 1:
            log.warning("Ignoring device list update with more than one element.")
            return

        item = items["item"].xml

        device_list: DeviceList = {}
        namespace: Optional[str] = None

        twomemo_device_list_elt = item.find(f"{{{twomemo.twomemo.NAMESPACE}}}devices")
        if twomemo_device_list_elt is not None:
            try:
                device_list = twomemo.etree.parse_device_list(twomemo_device_list_elt)
            except XMLSchemaValidationError:
                pass
            else:
                namespace = twomemo.twomemo.NAMESPACE

        oldmemo_device_list_elt = item.find(f"{{{oldmemo.oldmemo.NAMESPACE}}}list")
        if oldmemo_device_list_elt is not None:
            try:
                device_list = oldmemo.etree.parse_device_list(oldmemo_device_list_elt)
            except XMLSchemaValidationError:
                pass
            else:
                namespace = oldmemo.oldmemo.NAMESPACE

        if namespace is None:
            log.warning(f"Malformed device list update item: {ET.tostring(item, encoding='unicode')}")
            return

        session_manager = await self.get_session_manager()

        await session_manager.update_device_list(namespace, msg["from"].bare, device_list)

    async def _on_subscription_changed(self, presence: Presence) -> None:
        """
        Callback to handle presence subscription changes.

        Args:
            presence: The presence stanza triggering this callback.
        """

        jid = JID(presence["from"].bare)

        roster: RosterNode = self.xmpp.client_roster

        pep_enabled = roster.has_jid(jid) and roster[jid]["subscription"] == "both"

        log.debug(f"Subscription changed for {jid}; PEP enabled: {pep_enabled}")

        for namespace in [ twomemo.twomemo.NAMESPACE, oldmemo.oldmemo.NAMESPACE ]:
            subscribed = (await self.storage.load_primitive(
                f"/slixmpp/subscribed/{jid}/{namespace}",
                bool
            )).maybe(None)

            if subscribed is None:
                # This JID is not tracked.
                return

            # Remove manual subscriptions if PEP is enabled now
            if pep_enabled and subscribed:
                await self._unsubscribe(namespace, jid)

            # Add a manual subscription if PEP is disabled now
            if not pep_enabled and not subscribed:
                await self._subscribe(namespace, jid)

    async def _subscribe(self, namespace: str, jid: JID) -> None:
        """
        Manually subscribe to the device list pubsub node of the JID and track the subscription status.

        Args:
            namespace: The OMEMO version namespace (not the node).
            jid: The JID whose device list to manually subscribe to. Can be a bare (aka "userhost") JID but
                doesn't have to.
        """

        jid = JID(jid.bare)

        log.debug(f"Manually subscribing to {namespace} device list for {jid}")

        node = {
            twomemo.twomemo.NAMESPACE: TWOMEMO_DEVICE_LIST_NODE,
            oldmemo.oldmemo.NAMESPACE: OLDMEMO_DEVICE_LIST_NODE
        }.get(namespace, None)

        if node is None:
            raise UnknownNamespace(f"Unknown namespace during device list subscription: {namespace}")

        xep_0060: XEP_0060 = self.xmpp["xep_0060"]

        try:
            await xep_0060.subscribe(jid, node)
        except IqError as e:
            # Failure to subscribe is non-critical here, simply debug log the error (and don't update the
            # subscription status).
            log.debug(f"Couldn't subscribe to {namespace} device list of {jid.bare}", exc_info=e)
        else:
            await self.storage.store(f"/slixmpp/subscribed/{jid.bare}/{namespace}", True)

    async def _unsubscribe(self, namespace: str, jid: JID) -> None:
        """
        Manually unsubscribe from the device list pubsub node of the JID and track the subscription status.

        Args:
            namespace: The OMEMO version namespace (not the node).
            jid: The JID whose device list to manually unsubscribe from. Can be a bare (aka "userhost") JID
                but doesn't have to.
        """

        jid = JID(jid.bare)

        log.debug(f"Manually unsubscribing from {namespace} device list for {jid}")

        node = {
            twomemo.twomemo.NAMESPACE: TWOMEMO_DEVICE_LIST_NODE,
            oldmemo.oldmemo.NAMESPACE: OLDMEMO_DEVICE_LIST_NODE
        }.get(namespace, None)

        if node is None:
            raise UnknownNamespace(f"Unknown namespace during device list unsubscription: {namespace}")

        xep_0060: XEP_0060 = self.xmpp["xep_0060"]

        try:
            await xep_0060.unsubscribe(jid, node)
        except IqError as e:
            # Don't really care about any of the possible Iq error cases:
            # https://xmpp.org/extensions/xep-0060.html#subscriber-unsubscribe-error
            # Worst case we keep receiving updates we don't need.
            log.debug(f"Couldn't unsubscribe from {namespace} device list of {jid.bare}", exc_info=e)

        await self.storage.store(f"/slixmpp/subscribed/{jid.bare}/{namespace}", False)

    async def refresh_device_lists(self, jids: Set[JID], force_download: bool = False) -> None:
        """
        Ensure that up-to-date device lists for the JIDs are cached. This is done automatically by
        :meth:`encrypt_message`. You don't have to ever manually call this method, but you can do so for
        optimization reasons. For example, in a UI-based IM application, this method can be called when an
        OMEMO-enabled chat tab/window is opened, to be optimally prepared if the user decides to send an
        encrypted message.

        Args:
            jids: The JIDs whose device lists to refresh. Can be bare (aka "userhost") JIDs but don't have
                to.
            force_download: Force downloading the device list even if pubsub/PEP are enabled to automatically
                keep the cached device lists up-to-date.

        Raises:
            Exception: all exceptions raised by
                :meth:`~omemo.session_manager.SessionManager.refresh_device_lists` are forwarded as-is.
        """

        session_manager = await self.get_session_manager()
        storage = self.storage
        roster: RosterNode = self.xmpp.client_roster

        for jid in jids:
            jid = JID(jid.bare)

            if jid.bare == self.xmpp.boundjid.bare:
                # Skip ourselves
                continue

            # Track which namespaces require a manual refresh
            refresh_namespaces: Set[str] = \
                { twomemo.twomemo.NAMESPACE, oldmemo.oldmemo.NAMESPACE } if force_download else set()

            # PEP is "enabled" with mutual presence subscription and applies to all backends when enabled.
            pep_enabled = roster.has_jid(jid) and roster[jid]["subscription"] == "both"

            if not pep_enabled:
                # If PEP is not enabled, check whether manual subscription is enabled instead. Manual
                # subscription is tracked per-backend.
                for namespace in [ twomemo.twomemo.NAMESPACE, oldmemo.oldmemo.NAMESPACE ]:
                    subscribed = (await storage.load_primitive(
                        f"/slixmpp/subscribed/{jid.bare}/{namespace}",
                        bool
                    )).maybe(None)

                    if not subscribed:
                        # If not subscribed already (or the subscription status is unknown), manually
                        # subscribe to stay up-to-date automatically in the future. This trusts that servers,
                        # even if they support multi-subscribe, would not generate exact duplicate
                        # subscriptions with differing subscription ids.
                        await self._subscribe(namespace, jid)
                        refresh_namespaces.add(namespace)

            for namespace in refresh_namespaces:
                # Force-download the device lists that need a manual refresh
                try:
                    await session_manager.refresh_device_list(namespace, jid.bare)
                except omemo.DeviceListDownloadFailed as e:
                    log.debug(f"Couldn't manually fetch {namespace} device list, probably doesn't exist: {e}")

    async def encrypt_message(
        self,
        stanza: Message,
        recipient_jids: Union[JID, Set[JID]],
        identifier: Optional[str] = None
    ) -> Tuple[Dict[str, Message], FrozenSet[EncryptionError]]:
        """
        Encrypt a message stanza. Selects the optimal OMEMO protocol version for each recipient device.
        Twomemo encrypts the whole stanza using SCE, oldmemo encrypts only the body.

        Args:
            stanza: The stanza to encrypt. Must be associated with an XML stream that has message ids enabled.
            recipient_jids: The JID of the recipients. Can be bare (aka "userhost") JIDs but doesn't have to.
                A single JID can be used.
            identifier: A value that is passed on to :meth:`_devices_blindly_trusted` and
                :meth:`_prompt_manual_trust` in case a trust decision is required for any of the recipient
                devices. This value is not processed or altered, it is simply passed through. Refer to the
                documentation of :meth:`_devices_blindly_trusted` or :meth:`_prompt_manual_trust` for details.

        Returns:
            Encrypted messages ready to be sent and a set of non-critical errors encountered during
            encryption. The key is the messages dictionary is the OMEMO version namespace, the value is the
            encrypted message stanza for that OMEMO protocol version. The store hint is enabled on returned
            stanzas. Note that the ids (if any) of the original stanza are not preserved. This is to avoid
            duplicate id usage if the input stanza is encrypted multiple times for different protocol
            versions.

        Warning:
            Encrypted message stanzas for oldmemo consist of only the bare minimum: the encrypted body and the
            store hint. Other tags that can't be encrypted by oldmemo are _not_ automatically copied over from
            the source stanza; this has to be done manually afterwards if desired.

        Warning:
            Messages without a body are not considered for oldmemo encryption.

        Raises:
            Exception: all exceptions raised by :meth:`~omemo.session_manager.SessionManager.encrypt` are
                forwarded as-is.
        """

        if isinstance(recipient_jids, JID):
            recipient_jids = { recipient_jids }
        if not recipient_jids:
            raise ValueError("At least one JID must be specified")

        stream: Optional[XMLStream] = stanza.stream
        if stream is None or not getattr(stream, "use_message_ids", False):
            raise ValueError("Stanza not associated with a message id-enabled XML stream.")

        # Make sure all recipient device lists are available
        await self.refresh_device_lists(recipient_jids)

        recipient_bare_jids = frozenset({ recipient_jid.bare for recipient_jid in recipient_jids })

        # Prepare the plaintext for all protocol versions
        plaintexts: Dict[str, bytes] = {}

        # Here I would prepare the plaintext for omemo:2 using my SCE plugin ... IF I HAD ONE!!!

        # For oldmemo, only the body is encrypted
        body: Optional[str] = stanza.get("body", None)
        if body is not None:
            plaintexts[oldmemo.oldmemo.NAMESPACE] = body.encode("utf-8")

        log.debug(f"Plaintexts to encrypt: {plaintexts}")

        # Exit early if there's no plaintext to encrypt
        if len(plaintexts) == 0:
            return {}, frozenset()

        session_manager = await self.get_session_manager()

        messages, encryption_errors = await session_manager.encrypt(
            recipient_bare_jids,
            plaintexts,
            backend_priority_order=list(filter(
                lambda namespace: namespace in plaintexts,
                [ twomemo.twomemo.NAMESPACE, oldmemo.oldmemo.NAMESPACE ]
            )),
            identifier=identifier
        )

        encrypted_messages: Dict[str, Message] = {}

        for message in messages:
            namespace = message.namespace

            plaintext = plaintexts.get(namespace, None)
            message_elt: Optional[ET.Element] = None

            if namespace == twomemo.twomemo.NAMESPACE:
                message_elt = twomemo.etree.serialize_message(message)
            if namespace == oldmemo.oldmemo.NAMESPACE:
                message_elt = oldmemo.etree.serialize_message(message)

            if plaintext is None or message_elt is None:
                raise UnknownNamespace(f"OMEMO version namespace {namespace} unknown")

            stanza_copy = copy(stanza)
            stanza_copy.clear()
            stanza_copy["id"] = stream.new_id()
            stanza_copy.append(message_elt)
            stanza_copy["body"] = self.fallback_message
            stanza_copy.enable("store")

            encrypted_messages[namespace] = stanza_copy

            if stanza_copy.get_type() == "groupchat":
                # In contrast to one to one messages, MUC messages are reflected to the sender. Thus, the
                # sender usually does not add messages to their local message log when sending them, but when
                # the reflection is received. This approach does not pair well with OMEMO, since for security
                # reasons it is forbidden to encrypt messages for the own device. Thus, when the reflection of
                # an OMEMO message is received, it can't be decrypted and added to the local message log as
                # usual. To counteract this, the plaintext of outgoing messages sent to MUCs are cached by id,
                # such that when the reflection is received, the plaintext can be looked up from the cache and
                # returned as if it had just been decrypted.
                # TODO: The way reflections are handled currently, MUC messages that are encrypted for
                # multiple protocol versions will be reflected multiple times. Some logic is required to
                # filter duplicates, most likely prefering the newest protocol version's reflection and
                # discarding all others.
                self.__muc_reflection_cache[stanza_copy["id"]] = plaintext

        return encrypted_messages, encryption_errors

    async def decrypt_message(self, stanza: Message) -> Tuple[Message, DeviceInformation]:
        """
        Decrypt an OMEMO-encrypted message. Use :meth:`is_encrypted` to check whether a stanza contains an
        OMEMO-encrypted message. The original stanza is not modified by this method. For oldmemo, the optional
        fallback body is replaced with the decrypted content. For newmemo, the whole SCE stanza is returned.

        Args:
            stanza: The message stanza.

        Returns:
            The decrypted stanza and information about the sending device.

        Raises:
            ValueError: in case there is malformed data not caught be the XML schema validation.
            ValueError: in case a groupchat message is passed but XEP-0045 is not loaded.
            XMLSchemaValidationError: in case the element does not conform to the XML schema given in the
                specification.
            SenderNotFound: in case the public information about the sending device could not be found or is
                incomplete.
            Exception: all exceptions raised by :meth:`~omemo.session_manager.SessionManager.decrypt` are
                forwarded as-is.
        """

        xmpp: BaseXMPP = self.xmpp

        from_jid: JID = stanza.get_from()
        sender_bare_jid: str

        if stanza.get_type() == "groupchat":
            xep_0045: Optional[XEP_0045] = xmpp["xep_0045"]
            if not xep_0045:
                raise ValueError("Attempt to decrypt groupchat message but XEP-0045 is not loaded")

            real_jid = xep_0045.get_jid_property(JID(from_jid.bare), from_jid.resource, "jid")
            if real_jid is None:
                raise SenderNotFound(f"Couldn't find real JID of sender from groupchat JID {from_jid}")

            sender_bare_jid = JID(real_jid).bare
        else:
            sender_bare_jid = from_jid.bare

        session_manager = await self.get_session_manager()

        message: Optional[omemo.Message] = None
        encrypted_elt: Optional[ET.Element] = None

        twomemo_encrypted_elt = stanza.xml.findall(f"{{{twomemo.twomemo.NAMESPACE}}}encrypted")
        oldmemo_encrypted_elt = stanza.xml.findall(f"{{{oldmemo.oldmemo.NAMESPACE}}}encrypted")

        if len(twomemo_encrypted_elt) > 1:
            raise ValueError(
                f"Stanza contains multiple encrypted elements in the {twomemo.twomemo.NAMESPACE} namespace"
            )

        if len(oldmemo_encrypted_elt) > 1:
            raise ValueError(
                f"Stanza contains multiple encrypted elements in the {oldmemo.oldmemo.NAMESPACE} namespace"
            )

        if len(twomemo_encrypted_elt) + len(oldmemo_encrypted_elt) > 1:
            raise ValueError("Stanza contains a mix of encrypted elements in different OMEMO namespaces")

        if len(twomemo_encrypted_elt) == 1:
            encrypted_elt = twomemo_encrypted_elt[0]
            message = twomemo.etree.parse_message(encrypted_elt, sender_bare_jid)

        if len(oldmemo_encrypted_elt) == 1:
            encrypted_elt = oldmemo_encrypted_elt[0]
            message = await oldmemo.etree.parse_message(
                encrypted_elt,
                sender_bare_jid,
                xmpp.boundjid.bare,
                session_manager
            )

        if message is None or encrypted_elt is None:
            raise ValueError(f"No supported encrypted content found in stanza: {message}")

        plaintext: Optional[bytes]
        device_information: DeviceInformation
        try:
            plaintext, device_information = (await session_manager.decrypt(message))[:2]
        except MessageNotForUs:
            # If the message is not encrypted for us, check if it's a reflected MUC message that we have
            # cached.
            if stanza.get_type() != "groupchat":
                raise

            cached_plaintext = self.__muc_reflection_cache.pop(stanza["id"], None)
            if cached_plaintext is None:
                raise

            plaintext = cached_plaintext

            # It's a reflected MUC message, thus the sending device is us.
            device_information = (await session_manager.get_own_device_information())[0]

        if message.namespace == twomemo.twomemo.NAMESPACE:
            # Do SCE unpacking here
            raise NotImplementedError(f"SCE not supported yet. Plaintext: {plaintext!r}")

        if message.namespace == oldmemo.oldmemo.NAMESPACE:
            stanza = copy(stanza)

            # Remove all body elements from the original element, since those act as fallbacks in case the
            # encryption protocol is not supported
            del stanza["body"]

            if plaintext is not None:
                # Add the decrypted body
                stanza["body"] = plaintext.decode("utf-8")

        return stanza, device_information

    def is_encrypted(self, stanza: Message) -> Optional[str]:
        """
        Args:
            stanza: The stanza.

        Returns:
            The namespace of the OMEMO version this message is encrypted with, or `None` if the stanza is not
            encrypted with any supported version of OMEMO.
        """

        if stanza.xml.find(f"{{{twomemo.twomemo.NAMESPACE}}}encrypted") is not None:
            return twomemo.twomemo.NAMESPACE

        if stanza.xml.find(f"{{{oldmemo.oldmemo.NAMESPACE}}}encrypted") is not None:
            return oldmemo.oldmemo.NAMESPACE

        return None
