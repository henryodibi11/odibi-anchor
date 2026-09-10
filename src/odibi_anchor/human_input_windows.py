"""Interactive Windows owner-presence transport for governed approvals."""
from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes
from uuid import uuid4

from odibi_anchor.human_input import (
    HumanInputConfigurationError,
    HumanInputDeliveryError,
    HumanInputReply,
    HumanInputRequest,
)

_IDYES = 6
_IDNO = 7
_IDTIMEOUT = 32_000
_MESSAGE_BOX_FLAGS = 0x00000004 | 0x00000020 | 0x00002000 | 0x00010000 | 0x00040000
_APPROVAL_PATTERN = re.compile(r"(?:^|\n)Reply exactly: (APPROVE [0-9a-f]{64})$")


def _windows_username() -> str:
    """Return the current Windows security-context username."""
    if not hasattr(ctypes, "WinDLL"):
        raise HumanInputConfigurationError("Local owner approval requires Windows")
    buffer = ctypes.create_unicode_buffer(257)
    size = wintypes.DWORD(len(buffer))
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_user_name = advapi32.GetUserNameW
    get_user_name.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    get_user_name.restype = wintypes.BOOL
    if not get_user_name(buffer, ctypes.byref(size)) or not buffer.value.strip():
        raise HumanInputConfigurationError("Could not identify the interactive Windows account")
    return buffer.value.strip()


def _message_box(message: str, timeout_milliseconds: int) -> int:
    """Show a bounded native confirmation dialog and return its Windows result code."""
    if not hasattr(ctypes, "WinDLL"):
        raise HumanInputConfigurationError("Local owner approval requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        show = user32.MessageBoxTimeoutW
    except AttributeError as exc:
        raise HumanInputConfigurationError(
            "This Windows session does not provide bounded local owner approval"
        ) from exc
    show.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
        wintypes.WORD,
        wintypes.DWORD,
    ]
    show.restype = ctypes.c_int
    result = show(
        None,
        message,
        "Odibi Anchor — Owner Approval",
        _MESSAGE_BOX_FLAGS,
        0,
        timeout_milliseconds,
    )
    if result == 0:
        raise HumanInputDeliveryError(
            f"Windows could not show the owner approval dialog (error {ctypes.get_last_error()})"
        )
    return result


class LocalWindowsOwnerTransport:
    """Require a visible Yes/No action in the current Windows desktop session."""

    name = "local-windows-owner-presence"

    def __init__(self, owner_user_id: str) -> None:
        if not isinstance(owner_user_id, str) or not owner_user_id.strip():
            raise HumanInputConfigurationError("owner_user_id must be a non-empty string")
        self.owner_user_id = owner_user_id.strip()
        self._responses: dict[str, list[HumanInputReply]] = {}

    @classmethod
    def from_current_session(cls) -> LocalWindowsOwnerTransport:
        """Bind approval identity to the current Windows security context."""
        return cls(f"windows-account:{_windows_username()}")

    def deliver(self, request: HumanInputRequest) -> str:
        match = _APPROVAL_PATTERN.search(request.message)
        if match is None:
            raise HumanInputDeliveryError("Local owner request has no exact approval challenge")
        remaining_seconds = request.deadline_at - time.time()
        if remaining_seconds <= 0:
            raise HumanInputDeliveryError("Local owner request expired before display")
        prompt = (
            f"{request.message}\n\n"
            "This request came from a local agent. Click Yes only if you personally approve "
            "this exact memory transition. Click No to deny it."
        )
        result = _message_box(prompt, max(1, int(remaining_seconds * 1_000)))
        transport_ref = f"local-windows:{request.request_id}:{uuid4()}"
        if result in {_IDYES, _IDNO}:
            response = match.group(1) if result == _IDYES else "DECLINE"
            self._responses[transport_ref] = [
                HumanInputReply(
                    text=response,
                    user_id=self.owner_user_id,
                    message_id=f"{transport_ref}:response",
                    received_at=time.time(),
                )
            ]
        elif result == _IDTIMEOUT:
            self._responses[transport_ref] = []
        else:
            raise HumanInputDeliveryError("Windows owner approval dialog returned an invalid result")
        return transport_ref

    def replies(self, transport_ref: str) -> list[HumanInputReply]:
        try:
            return list(self._responses[transport_ref])
        except KeyError as exc:
            raise HumanInputDeliveryError("Unknown local owner approval reference") from exc


__all__ = ["LocalWindowsOwnerTransport"]
