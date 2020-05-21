import curses
import logging
import os
import threading
from datetime import datetime
from functools import partial
from signal import SIGWINCH, signal
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, Optional

from tg import config
from tg.models import Model
from tg.msg import MsgProxy
from tg.tdlib import Tdlib
from tg.utils import (
    get_duration,
    get_video_resolution,
    get_waveform,
    handle_exception,
    is_yes,
    notify,
    suspend,
)
from tg.views import View

log = logging.getLogger(__name__)

MSGS_LEFT_SCROLL_THRESHOLD = 10


# start scrolling to next page when number of the msgs left is less than value.
# note, that setting high values could lead to situations when long msgs will
# be removed from the display in order to achive scroll threshold. this could
# cause blan areas on the msg display screen
MSGS_LEFT_SCROLL_THRESHOLD = 2

key_bind_handler = Callable[[Any], Any]


class Controller:
    """
    # MVC
    # Model is data from telegram
    # Controller handles keyboad events
    # View is terminal vindow
    """

    def __init__(self, model: Model, view: View, tg: Tdlib) -> None:
        self.model = model
        self.view = view
        self.lock = threading.Lock()
        self.tg = tg
        self.chat_size = 0.5
        signal(SIGWINCH, self.resize_handler)

        self.chat_bindings: Dict[str, key_bind_handler] = {
            "q": lambda _: "QUIT",
            "l": self.handle_msgs,
            "j": self.next_chat,
            "^N": self.next_chat,
            "k": self.prev_chat,
            "^P": self.prev_chat,
            "J": lambda _: self.next_chat(10),
            "K": lambda _: self.prev_chat(10),
            "gg": self.first_chat,
            "bp": self.breakpoint,
            "u": self.toggle_unread,
            "p": self.toggle_pin,
            "m": self.toggle_mute,
            "r": self.read_msgs,
        }

        self.msg_bindings: Dict[str, key_bind_handler] = {
            "q": lambda _: "QUIT",
            "h": lambda _: "BACK",
            "^D": lambda _: "BACK",
            "]": self.next_chat,
            "[": self.prev_chat,
            "J": lambda _: self.next_msg(10),
            "K": lambda _: self.prev_msg(10),
            "j": self.next_msg,
            "^N": self.next_msg,
            "k": self.prev_msg,
            "^P": self.prev_msg,
            "G": self.jump_bottom,
            "dd": self.delete_msg,
            "D": self.download_current_file,
            "l": self.open_current_msg,
            "sd": lambda _: self.send_file(self.tg.send_doc),
            "sp": lambda _: self.send_file(self.tg.send_photo),
            "sa": lambda _: self.send_file(self.tg.send_audio),
            "sv": self.send_video,
            "v": self.send_voice,
            "e": self.edit_msg,
            "i": self.write_short_msg,
            "a": self.write_short_msg,
            "I": self.write_long_msg,
            "A": self.write_long_msg,
            "bp": self.breakpoint,
            " ": self.select_msg,
            "^[": self.discard_selected_msgs,  # esc
            "y": self.copy_msgs,
            "p": self.forward_msgs,
        }

    def forward_msgs(self, _: int):
        chat_id = self.model.chats.id_by_index(self.model.current_chat)
        if not self.model.yanked_msgs:
            return
        from_chat_id, msg_ids = self.model.yanked_msgs
        self.tg.forward_msgs(chat_id, from_chat_id, msg_ids)
        self.present_info(f"Forwarded {len(msg_ids)} messages")

    def copy_msgs(self, _: int):
        # can_be_forwarded
        chat_id = self.model.chats.id_by_index(self.model.current_chat)
        msg_ids = self.model.selected[chat_id]
        if not msg_ids:
            self.present_error("No msgs selected")
            return
        self.model.yanked_msgs = (chat_id, msg_ids)
        self.discard_selected_msgs(0)
        self.present_info(f"Copied {len(msg_ids)} messages")

    def select_msg(self, _: int):
        chat_id = self.model.chats.id_by_index(self.model.current_chat)
        msg = MsgProxy(self.model.current_msg)

        if msg.msg_id in self.model.selected[chat_id]:
            self.model.selected[chat_id].remove(msg.msg_id)
        else:
            self.model.selected[chat_id].append(msg.msg_id)
        self.model.next_msg(1)
        self.refresh_msgs()
        self.present_info("Removed selections")

    def discard_selected_msgs(self, _: int):
        chat_id = self.model.chats.id_by_index(self.model.current_chat)
        self.model.selected[chat_id] = []
        self.refresh_msgs()

    def jump_bottom(self, _: int):
        log.info("jump_bottom:")
        if self.model.jump_bottom():
            self.refresh_msgs()

    def handle_msgs(self, _: int):
        rc = self.handle(self.msg_bindings, 0.2)
        if rc == "QUIT":
            return rc
        self.chat_size = 0.5
        self.resize()

    def next_chat(self, rf: int):
        if self.model.next_chat(rf):
            self.render()

    def prev_chat(self, rf: int):
        if self.model.prev_chat(rf):
            self.render()

    def first_chat(self, _: int):
        if self.model.first_chat():
            self.render()

    def toggle_unread(self, _: int):
        chat = self.model.chats.chats[self.model.current_chat]
        chat_id = chat["id"]
        toggle = not chat["is_marked_as_unread"]
        self.tg.toggle_chat_is_marked_as_unread(chat_id, toggle)
        self.render()

    def read_msgs(self, _: int):
        chat = self.model.chats.chats[self.model.current_chat]
        chat_id = chat["id"]
        msg_id = chat["last_message"]["id"]
        self.tg.view_messages(chat_id, [msg_id])
        self.render()

    def toggle_mute(self, _: int):
        # TODO: if it's msg to yourself, do not change its
        # notification setting, because we can't by documentation,
        # instead write about it in status
        chat = self.model.chats.chats[self.model.current_chat]
        chat_id = chat["id"]
        if self.model.is_me(chat_id):
            self.present_error("You can't mute Saved Messages")
            return
        notification_settings = chat["notification_settings"]
        if notification_settings["mute_for"]:
            notification_settings["mute_for"] = 0
        else:
            notification_settings["mute_for"] = 2147483647
        self.tg.set_chat_nottification_settings(chat_id, notification_settings)
        self.render()

    def toggle_pin(self, _: int):
        chat = self.model.chats.chats[self.model.current_chat]
        chat_id = chat["id"]
        toggle = not chat["is_pinned"]
        self.tg.toggle_chat_is_pinned(chat_id, toggle)
        self.render()

    def next_msg(self, rf: int):
        if self.model.next_msg(rf):
            self.refresh_msgs()

    def prev_msg(self, rf: int):
        if self.model.prev_msg(rf):
            self.refresh_msgs()

    def breakpoint(self, _: int):
        with suspend(self.view):
            breakpoint()

    def write_short_msg(self, _: int):
        # write new message
        if msg := self.view.status.get_input():
            self.model.send_message(text=msg)
            self.present_info("Message sent")
        else:
            self.present_info("Message wasn't sent")

    def send_video(self, _: int):
        file_path = self.view.status.get_input()
        if not file_path or not os.path.isfile(file_path):
            return
        chat_id = self.model.chats.id_by_index(self.model.current_chat)
        if not chat_id:
            return
        width, height = get_video_resolution(file_path)
        duration = get_duration(file_path)
        self.tg.send_video(file_path, chat_id, width, height, duration)

    def delete_msg(self, _: int):
        if self.model.delete_msg():
            self.refresh_msgs()
            self.present_info("Message deleted")

    def send_file(self, send_file_fun, *args, **kwargs):
        file_path = self.view.status.get_input()
        if file_path and os.path.isfile(file_path):
            chat_id = self.model.chats.id_by_index(self.model.current_chat)
            send_file_fun(file_path, chat_id, *args, **kwargs)
            self.present_info("File sent")

    def send_voice(self, _: int):
        file_path = f"/tmp/voice-{datetime.now()}.oga"
        with suspend(self.view) as s:
            s.call(config.record_cmd.format(file_path=file_path))
        resp = self.view.status.get_input(
            f"Do you want to send recording: {file_path}? [Y/n]"
        )
        if not is_yes(resp):
            self.present_info("Voice message discarded")
            return

        if not os.path.isfile(file_path):
            self.present_info(f"Can't load recording file {file_path}")
            return

        chat_id = self.model.chats.id_by_index(self.model.current_chat)
        if not chat_id:
            return
        duration = get_duration(file_path)
        waveform = get_waveform(file_path)
        self.tg.send_voice(file_path, chat_id, duration, waveform)
        self.present_info(f"Sent voice msg: {file_path}")

    def run(self) -> None:
        try:
            self.handle(self.chat_bindings, 0.5)
        except Exception:
            log.exception("Error happened in main loop")

    def download_current_file(self, _: int):
        msg = MsgProxy(self.model.current_msg)
        log.debug("Downloading msg: %s", msg.msg)
        file_id = msg.file_id
        if not file_id:
            self.present_info("File can't be downloaded")
            return
        self.download(file_id, msg["chat_id"], msg["id"])
        self.present_info("File started downloading")

    def download(self, file_id: int, chat_id: int, msg_id: int):
        log.info("Downloading file: file_id=%s", file_id)
        self.model.downloads[file_id] = (chat_id, msg_id)
        self.tg.download_file(file_id=file_id)
        log.info("Downloaded: file_id=%s", file_id)

    def open_current_msg(self, _: int):
        msg = MsgProxy(self.model.current_msg)
        if msg.is_text:
            with NamedTemporaryFile("w", suffix=".txt") as f:
                f.write(msg.text_content)
                f.flush()
                with suspend(self.view) as s:
                    s.open_file(f.name)
            return

        path = msg.local_path
        if not path:
            self.present_info("File should be downloaded first")
            return
        chat_id = self.model.chats.id_by_index(self.model.current_chat)
        if not chat_id:
            return
        self.tg.open_message_content(chat_id, msg.msg_id)
        with suspend(self.view) as s:
            s.open_file(path)

    def present_error(self, msg: str):
        return self.update_status("Error", msg)

    def present_info(self, msg: str):
        return self.update_status("Info", msg)

    def update_status(self, level: str, msg: str):
        with self.lock:
            self.view.status.draw(f"{level}: {msg}")

    def edit_msg(self, _: int):
        msg = MsgProxy(self.model.current_msg)
        log.info("Editing msg: %s", msg.msg)
        if not self.model.is_me(msg.sender_id):
            return self.present_error("You can edit only your messages!")
        if not msg.is_text:
            return self.present_error("You can edit text messages only!")
        if not msg.can_be_edited:
            return self.present_error("Meessage can't be edited!")

        with NamedTemporaryFile("r+", suffix=".txt") as f, suspend(
            self.view
        ) as s:
            f.write(msg.text_content)
            f.flush()
            s.call(f"{config.editor} {f.name}")
            with open(f.name) as f:
                if text := f.read().strip():
                    self.model.edit_message(text=text)
                    self.present_info("Message edited")

    def write_long_msg(self, _: int):
        with NamedTemporaryFile("r+", suffix=".txt") as f, suspend(
            self.view
        ) as s:
            s.call(config.long_msg_cmd.format(file_path=f.name))
            with open(f.name) as f:
                if msg := f.read().strip():
                    self.model.send_message(text=msg)
                    self.present_info("Message sent")

    def resize_handler(self, signum, frame):
        curses.endwin()
        self.view.stdscr.refresh()
        self.resize()

    def resize(self):
        rows, cols = self.view.stdscr.getmaxyx()
        # If we didn't clear the screen before doing this,
        # the original window contents would remain on the screen
        # and we would see the window text twice.
        self.view.stdscr.erase()
        self.view.stdscr.noutrefresh()

        self.view.chats.resize(rows, cols, self.chat_size)
        self.view.msgs.resize(rows, cols, 1 - self.chat_size)
        self.view.status.resize(rows, cols)
        self.render()

    def handle(self, key_bindings: Dict[str, key_bind_handler], size: float):
        self.chat_size = size
        self.resize()

        while True:
            repeat_factor, keys = self.view.get_keys()
            handler = key_bindings.get(keys, lambda _: None)
            res = handler(repeat_factor)
            if res == "QUIT":
                return res
            elif res == "BACK":
                return res

    def render(self) -> None:
        with self.lock:
            # using lock here, because render is used from another
            # thread by tdlib python wrapper
            page_size = self.view.chats.h
            chats = self.model.get_chats(
                self.model.current_chat, page_size, MSGS_LEFT_SCROLL_THRESHOLD
            )
            selected_chat = min(
                self.model.current_chat, page_size - MSGS_LEFT_SCROLL_THRESHOLD
            )

            self.view.chats.draw(selected_chat, chats)
            self.refresh_msgs()
            self.view.status.draw()

    def refresh_msgs(self) -> None:
        current_msg_idx = self.model.get_current_chat_msg_idx()
        if current_msg_idx is None:
            return
        msgs = self.model.fetch_msgs(
            current_position=current_msg_idx,
            page_size=self.view.msgs.h,
            msgs_left_scroll_threshold=MSGS_LEFT_SCROLL_THRESHOLD,
        )
        self.view.msgs.draw(current_msg_idx, msgs, MSGS_LEFT_SCROLL_THRESHOLD)

    def _notify_for_message(self, chat_id: int, msg: MsgProxy):
        # do not notify, if muted
        # TODO: optimize
        chat = None
        for chat in self.model.chats.chats:
            if chat_id == chat["id"]:
                break

        # TODO: handle cases when all chats muted on global level
        if chat and chat["notification_settings"]["mute_for"]:
            return

        # notify
        if self.model.is_me(msg["sender_user_id"]):
            return
        user = self.model.users.get_user(msg.sender_id)
        name = f"{user['first_name']} {user['last_name']}"

        text = msg.text_content if msg.is_text else msg.content_type
        notify(text, title=name)

    def _refresh_current_chat(self, current_chat_id: Optional[int]):
        if current_chat_id is None:
            return
        # TODO: we can create <index> for chats, it's faster than sqlite anyway
        # though need to make sure that creatinng index is atomic operation
        # requires locks for read, until index and chats will be the same
        for i, chat in enumerate(self.model.chats.chats):
            if chat["id"] == current_chat_id:
                self.model.current_chat = i
                break
        self.render()
