"""Unit tests for the RFID capability probe (RfidProbe)."""
from custom_components.unite_evcc_bridge.control import RfidProbe


def test_fresh_probe_wants_one_probe():
    assert RfidProbe().want_probe is True


def test_success_latches_supported_and_clears_strikes():
    probe = RfidProbe()
    probe.note_transport_error()
    probe.note_ok()
    assert probe.supported is True
    assert probe.transport_strikes == 0
    assert probe.want_probe is True


def test_clean_refusal_disables_until_reconnect():
    probe = RfidProbe()
    probe.note_unsupported()
    assert probe.want_probe is False
    probe.reset_on_reconnect()
    assert probe.want_probe is True


def test_first_transport_error_only_quiets_until_reconnect():
    probe = RfidProbe()
    assert probe.note_transport_error() is False
    assert probe.want_probe is False
    assert probe.session_disabled is False
    probe.reset_on_reconnect()
    assert probe.want_probe is True


def test_second_transport_error_disables_for_the_session():
    probe = RfidProbe()
    assert probe.note_transport_error() is False
    probe.reset_on_reconnect()
    assert probe.note_transport_error() is True
    assert probe.session_disabled is True
    assert probe.want_probe is False
    probe.reset_on_reconnect()
    assert probe.want_probe is False
