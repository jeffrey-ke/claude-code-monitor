"""The _alive identity check that kills multi-login-node orange ghosts."""
import os

from ccstatus import _alive, _proc_identity_ok

UID = 1000


def test_other_uid_is_dead_even_if_claudeish():
    # the recycling case: a stranger's process landed on our old pid
    assert _proc_identity_ok("claude", "claude --serve", UID + 1, UID) is False


def test_our_claude_comm():
    assert _proc_identity_ok("claude", "", UID, UID) is True


def test_our_node_comm():
    # the CLI runs on node — comm may show the runtime, not the entrypoint
    assert _proc_identity_ok("node", "", UID, UID) is True


def test_our_cmdline_mentions_claude():
    assert _proc_identity_ok("MainThread", "/usr/bin/node /home/x/.local/bin/claude", UID,
                             UID) is True


def test_our_unrelated_process_is_dead():
    # same-uid recycling (our own vim grabbed the pid) — strict check says dead
    assert _proc_identity_ok("vim", "vim notes.md", UID, UID) is False


def test_empty_identity_is_dead():
    assert _proc_identity_ok("", "", UID, UID) is False
    assert _proc_identity_ok(None, None, UID, UID) is False


def test_case_insensitive():
    assert _proc_identity_ok("Claude", "", UID, UID) is True
    assert _proc_identity_ok("x", "RUN Claude NOW", UID, UID) is True


# ---------------------------------------------------------------- _alive integration

def test_alive_none_pid_stays_vouched():
    assert _alive(None) is True


def test_alive_vanished_pid_is_dead():
    assert _alive(2 ** 22 - 3) is False        # beyond any default pid_max


def test_alive_own_python_is_not_claude():
    # this very test process: exists, our uid, but not claude-ish ⇒ dead
    assert _alive(os.getpid()) is False


def test_alive_init_is_not_ours():
    assert _alive(1) is False                  # pid 1: exists, root-owned ⇒ dead
