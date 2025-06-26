from argparse import ArgumentParser
import asyncio
from getpass import getpass
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, FrozenSet, Literal, NamedTuple, Optional, Set, Union

from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import DeviceInformation, JSONType

from slixmpp.clientxmpp import ClientXMPP
from slixmpp.jid import JID  # pylint: disable=no-name-in-module
from slixmpp.plugins import register_plugin  # type: ignore[attr-defined]
from slixmpp.plugins.xep_0045 import XEP_0045  # type: ignore[attr-defined]
from slixmpp.stanza import Message
from slixmpp.xmlstream.handler import CoroutineCallback
from slixmpp.xmlstream.matcher import MatchXPath

from slixmpp_omemo import TrustLevel, XEP_0384


log = logging.getLogger(__name__)


class StorageImpl(Storage):
    """
    Example storage implementation that stores all data in a single JSON file.
    """

    def __init__(self, json_file_path: Path) -> None:
        super().__init__()

        self.__json_file_path = json_file_path
        self.__data: Dict[str, JSONType] = {}
        try:
            with open(self.__json_file_path, encoding="utf8") as f:
                self.__data = json.load(f)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    async def _load(self, key: str) -> Maybe[JSONType]:
        if key in self.__data:
            return Just(self.__data[key])

        return Nothing()

    async def _store(self, key: str, value: JSONType) -> None:
        self.__data[key] = value
        with open(self.__json_file_path, "w", encoding="utf8") as f:
            json.dump(self.__data, f)

    async def _delete(self, key: str) -> None:
        self.__data.pop(key, None)
        with open(self.__json_file_path, "w", encoding="utf8") as f:
            json.dump(self.__data, f)


class PluginCouldNotLoad(Exception):
    pass


class XEP_0384Impl(XEP_0384):  # pylint: disable=invalid-name
    """
    Example implementation of the OMEMO plugin for Slixmpp.
    """

    default_config = {
        "fallback_message": "This message is OMEMO encrypted.",
        "json_file_path": None
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # Just the type definition here
        self.__storage: Storage

    def plugin_init(self) -> None:
        if not self.json_file_path:
            raise PluginCouldNotLoad("JSON file path not specified.")

        self.__storage = StorageImpl(Path(self.json_file_path))

        super().plugin_init()

    @property
    def storage(self) -> Storage:
        return self.__storage

    @property
    def _btbv_enabled(self) -> bool:
        return True

    async def _devices_blindly_trusted(
        self,
        blindly_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str]
    ) -> None:
        log.info(f"[{identifier}] Devices trusted blindly: {blindly_trusted}")

    async def _prompt_manual_trust(
        self,
        manually_trusted: FrozenSet[DeviceInformation],
        identifier: Optional[str]
    ) -> None:
        # Since BTBV is enabled and we don't do any manual trust adjustments in the example, this method
        # should never be called. All devices should be automatically trusted blindly by BTBV.

        # To show how a full implementation could look like, the following code will prompt for a trust
        # decision using `input`:
        session_mananger = await self.get_session_manager()

        for device in manually_trusted:
            while True:
                answer = input(f"[{identifier}] Trust the following device? (yes/no) {device}")
                if answer in { "yes", "no" }:
                    await session_mananger.set_trust(
                        device.bare_jid,
                        device.identity_key,
                        TrustLevel.TRUSTED.value if answer == "yes" else TrustLevel.DISTRUSTED.value
                    )
                    break
                print("Please answer yes or no.")


register_plugin(XEP_0384Impl)


class MUCJoinInfo(NamedTuple):
    room_jid: JID
    nick: str


class OmemoEchoClient(ClientXMPP):
    """
    A simple Slixmpp bot that will echo encrypted messages it receives.
    Works in one-to-one chats as well as in MUCs.

    For details on how to build a client with Slixmpp, look at examples in the Slixmpp repository.
    """

    def __init__(self, jid: str, password: str, muc_join_info: Optional[MUCJoinInfo]) -> None:
        super().__init__(jid, password)

        self.muc_join_info = muc_join_info

        self.add_event_handler("session_start", self.start)
        self.register_handler(CoroutineCallback(
            "Messages",
            MatchXPath(f"{{{self.default_ns}}}message"),
            self.message_handler  # type: ignore[arg-type]
        ))

    async def start(self, _event: Any) -> None:
        """
        Process the session_start event.

        Typical actions for the session_start event are requesting the roster and broadcasting an initial
        presence stanza.

        Args:
            event: An empty dictionary. The session_start event does not provide any additional data.
        """

        self.send_presence()
        await self.get_roster()  # type: ignore[no-untyped-call]

        xep_0045: Optional[XEP_0045] = self["xep_0045"]
        if xep_0045 is not None and self.muc_join_info is not None:
            # Started as a task as a workaround for https://codeberg.org/poezio/slixmpp/issues/3660
            asyncio.create_task(xep_0045.join_muc_wait(self.muc_join_info.room_jid, self.muc_join_info.nick))

    async def message_handler(self, stanza: Message) -> None:
        """
        Process incoming message stanzas. Be aware that this also includes MUC messages and error messages. It
        is usually a good idea to check the messages's type before processing or sending replies.

        Args:
            msg: The received message stanza. See the documentation for stanza objects and the Message stanza
                to see how it may be used.
        """

        xep_0045: Optional[XEP_0045] = self["xep_0045"]
        xep_0384: XEP_0384 = self["xep_0384"]

        mfrom: JID = stanza["from"]

        mtype = stanza["type"]
        if mtype not in { "chat", "normal", "groupchat" }:
            return

        is_muc_reflection = False
        if mtype == "groupchat":
            if xep_0045 is None:
                log.warning("Ignoring MUC message while MUC plugin is not loaded.")
                return

            # Save the sender's nick and modify mfrom to just be the MUC JID
            mfrom_nick = mfrom.resource
            mfrom = JID(mfrom.bare)

            # Find the real JID of the sender
            real_mfrom_str: Optional[str] = xep_0045.get_jid_property(JID(mfrom.bare), mfrom_nick, "jid")
            if real_mfrom_str is None:
                self.plain_reply(
                    mfrom,
                    mtype,
                    "This bot cannot be used in semi-anonymous MUCs."
                )
                return

            # Check if this message is a MUC reflection
            if JID(real_mfrom_str) == self.boundjid:
                is_muc_reflection = True

        namespace = xep_0384.is_encrypted(stanza)
        if namespace is None:
            if not stanza["body"]:
                # This is the case for things like read markers, ignore those.
                return

            # Don't respond to reflected MUC messages
            if is_muc_reflection:
                log.debug(f"Ignoring reflected unencrypted MUC message: {stanza['body']}")
                return

            self.plain_reply(
                mfrom,
                mtype,
                f"Unencrypted message or unsupported message encryption: {stanza['body']}"
            )
            return

        log.debug(f"Message in namespace {namespace} received: {stanza}")

        try:
            message, device_information = await xep_0384.decrypt_message(stanza)

            log.debug(f"Information about sender: {device_information}")

            if not message["body"]:
                # This is the case for things like read markers, ignore those.
                return

            # Don't respond to reflected MUC messages. Cool to see though if message reflection in encrypted
            # MUCs works correctly
            if is_muc_reflection:
                log.info(f"Ignoring reflected encrypted MUC message: {message['body']}")
                return

            await self.encrypted_reply(mfrom, mtype, message)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warning("Exception while handling encrypted message", exc_info=True)
            self.plain_reply(mfrom, mtype, f"Error {type(e).__name__}: {str(e)}")

    def plain_reply(self, mto: JID, mtype: Literal["chat", "normal", "groupchat"], reply: str) -> None:
        """
        Helper to reply with plain messages.

        Args:
            mto: The recipient JID.
            mtype: The message type.
            reply: The text content of the reply.
        """

        stanza = self.make_message(mto=mto, mtype=mtype)
        stanza["body"] = reply
        stanza.send()

    async def encrypted_reply(
        self,
        mto: JID,
        mtype: Literal["chat", "normal", "groupchat"],
        reply: Union[Message, str]
    ) -> None:
        """
        Helper to reply with encrypted messages.

        Args:
            mto: The recipient JID.
            mtype: The message type.
            reply: Either the message stanza to encrypt and reply with, or the text content of the reply.
        """

        xep_0384: XEP_0384 = self["xep_0384"]

        if isinstance(reply, str):
            reply_body = reply
            reply = self.make_message(mto=mto, mtype=mtype)
            reply["body"] = reply_body

        reply.set_to(mto)
        reply.set_from(self.boundjid)

        encrypt_for: Set[JID] = { mto }
        if mtype == "groupchat":
            # In a MUC, encrypt for all participants
            xep_0045: XEP_0045 = self["xep_0045"]
            encrypt_for = {
                JID(xep_0045.get_jid_property(mto, nick, "jid"))
                for nick
                in xep_0045.get_roster(mto)
            }

        messages, encryption_errors = await xep_0384.encrypt_message(reply, encrypt_for)

        if len(encryption_errors) > 0:
            log.info(f"There were non-critical errors during encryption: {encryption_errors}")

        for namespace, message in messages.items():
            message["eme"]["namespace"] = namespace
            message["eme"]["name"] = self["xep_0380"].mechanisms[namespace]
            message.send()


def main() -> None:
    # Set up the command line argument parser
    parser = ArgumentParser(description=OmemoEchoClient.__doc__)

    parser.add_argument("-u", "--username", dest="username", help="account username (JID)")
    parser.add_argument("-p", "--password", dest="password", help="account password")
    parser.add_argument(
        "-d", "--data-path",
        dest="json_file_path",
        type=Path,
        default=Path.home() / "omemo-echo-client.json",
        help="Path of the JSON file to hold the OMEMO data for this account. Note that a data file can not be"
             " shared between multiple accounts."
    )

    parser.add_argument(
        "-r", "--room-jid",
        dest="room_jid",
        help="JID of a MUC to join. --nick must also be set if this is used."
    )

    parser.add_argument(
        "-n", "--nick",
        dest="nick",
        help="Nickname to use in the MUC specified by --room-jid."
    )

    parser.add_argument(
        "-v", "--verbose",
        dest="log_level",
        action="count",
        default=0,
        help="logging verbosity (WARNING (default), INFO (-v), DEBUG (-vv))"
    )

    args = parser.parse_args()

    logging.basicConfig(level=[
        logging.WARNING,
        logging.INFO
    ][min(args.log_level, 1)])

    log.setLevel([
        logging.WARNING,
        logging.INFO,
        logging.DEBUG
    ][min(args.log_level, 2)])

    muc_join_info: Optional[MUCJoinInfo] = None
    if args.room_jid is not None:
        if args.nick is None:
            raise ValueError("Please set both the room JID of a MUC to join and the nickname to use.")
        muc_join_info = MUCJoinInfo(room_jid=JID(args.room_jid), nick=args.nick)

    # Ask for username and password if not supplied via cli args
    if args.username is None:
        args.username = input("Username: ")
    if args.password is None:
        args.password = getpass("Password: ")

    # Setup the OmemoEchoClient and register plugins. Note that while plugins may have interdependencies, the
    # order in which you register them does not matter.

    xmpp = OmemoEchoClient(args.username, args.password, muc_join_info)
    xmpp.register_plugin("xep_0045")  # Multi-User Chat
    xmpp.register_plugin("xep_0199")  # XMPP Ping
    xmpp.register_plugin("xep_0380")  # Explicit Message Encryption
    xmpp.register_plugin(
        "xep_0384",
        { "json_file_path": args.json_file_path },
        module=sys.modules[__name__]
    )  # OMEMO

    # Connect to the XMPP server and start processing XMPP stanzas.
    xmpp.connect()
    asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    main()
