"""Unit tests for ``receivers.cfg.device_probe``.

The per-protocol extractors are mocked so no network is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.device_probe import (
    PROBE_TYPE_CHOICES,
    ProbeError,
    ProbeIncompleteError,
    ProbeNotIdentifiedError,
    ProbeUnreachableError,
    ReceiverIdentity,
    parse_host_port,
    probe_receiver,
    to_subtype_attrs,
)

# ---------------------------------------------------------------------------
# parse_host_port
# ---------------------------------------------------------------------------


def test_parse_host_port_host_only() -> None:
    assert parse_host_port("192.168.20.1") == ("192.168.20.1", None)


def test_parse_host_port_with_port() -> None:
    assert parse_host_port("192.168.20.1:28784") == ("192.168.20.1", 28784)


def test_parse_host_port_hostname() -> None:
    assert parse_host_port("polarx-bench.local:28784") == (
        "polarx-bench.local",
        28784,
    )


def test_parse_host_port_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        parse_host_port("")


def test_parse_host_port_rejects_non_integer_port() -> None:
    with pytest.raises(ValueError, match="integer"):
        parse_host_port("host:abc")


def test_parse_host_port_rejects_missing_host() -> None:
    with pytest.raises(ValueError):
        parse_host_port(":28784")


# ---------------------------------------------------------------------------
# PROBE_TYPE_CHOICES contract
# ---------------------------------------------------------------------------


def test_probe_type_choices_includes_auto_and_all_families() -> None:
    assert set(PROBE_TYPE_CHOICES) == {
        "auto",
        "polarx5",
        "netr9",
        "netrs",
        "netr5",
        "g10",
    }


# ---------------------------------------------------------------------------
# _probe_polarx5 (via PROBE_STRATEGIES["polarx5"])
# ---------------------------------------------------------------------------


@patch("receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor")
def test_polarx5_probe_happy_path(mock_extractor_cls: MagicMock) -> None:
    inst = mock_extractor_cls.return_value
    inst._query_receiver_setup.return_value = {
        "receiver_model": "PolaRx5",
        "firmware_version": "5.5.0",
        "serial_number": "SN12345",
        "marker_name": "BENC",
    }
    identity = probe_receiver(
        "192.168.20.1", 28784, probe_type="polarx5", station_id_hint="BENC"
    )
    assert identity.probe_type == "polarx5"
    assert identity.serial == "SN12345"
    assert identity.model_raw == "PolaRx5"
    assert identity.firmware_version == "5.5.0"
    assert identity.marker_name == "BENC"
    assert identity.partial is False
    assert identity.subtype == "gnss_receiver"


@patch("receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor")
def test_polarx5_probe_unreachable_raises(mock_extractor_cls: MagicMock) -> None:
    inst = mock_extractor_cls.return_value
    inst._query_receiver_setup.side_effect = ConnectionRefusedError("nope")
    with pytest.raises(ProbeUnreachableError):
        probe_receiver("10.255.255.1", None, probe_type="polarx5")


@patch("receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor")
def test_polarx5_probe_empty_response_raises_not_identified(
    mock_extractor_cls: MagicMock,
) -> None:
    inst = mock_extractor_cls.return_value
    inst._query_receiver_setup.return_value = None
    with pytest.raises(ProbeNotIdentifiedError):
        probe_receiver("host", None, probe_type="polarx5")


# ---------------------------------------------------------------------------
# _probe_trimble (NetR9 / NetRS / NetR5)
# ---------------------------------------------------------------------------


@patch("receivers.health.trimble_http_extractor.TrimbleHTTPExtractor")
def test_trimble_netr9_probe(mock_extractor_cls: MagicMock) -> None:
    inst = mock_extractor_cls.return_value
    inst._fetch_system_info.return_value = {
        "serial_number": "5331K12345",
        "firmware_version": "5.45",
        "antenna_type": "TRM57971.00",
    }
    identity = probe_receiver("host", None, probe_type="netr9")
    assert identity.probe_type == "netr9"
    assert identity.serial == "5331K12345"
    assert identity.model_raw == "NetR9"
    assert identity.firmware_version == "5.45"
    assert identity.partial is False
    # Trimble extractor must have been told it's a NetR9.
    _, kwargs = mock_extractor_cls.call_args
    assert kwargs["receiver_type"] == "NetR9"


@patch("receivers.health.trimble_http_extractor.TrimbleHTTPExtractor")
def test_trimble_netr5_probe_partial_when_firmware_missing(
    mock_extractor_cls: MagicMock,
) -> None:
    """NetR5 only returns serial — firmware/antenna endpoints fail. Identity
    should be marked ``partial`` so the CLI can prompt for the missing fields."""
    inst = mock_extractor_cls.return_value
    inst._fetch_system_info.return_value = {"serial_number": "NETR5-001"}
    identity = probe_receiver("host", None, probe_type="netr5")
    assert identity.partial is True
    assert identity.firmware_version is None
    assert identity.serial == "NETR5-001"


@patch("receivers.health.trimble_http_extractor.TrimbleHTTPExtractor")
def test_trimble_probe_unreachable_raises(mock_extractor_cls: MagicMock) -> None:
    inst = mock_extractor_cls.return_value
    inst._fetch_system_info.side_effect = OSError("network down")
    with pytest.raises(ProbeUnreachableError):
        probe_receiver("host", None, probe_type="netrs")


# ---------------------------------------------------------------------------
# _probe_g10
# ---------------------------------------------------------------------------


@patch("receivers.health.g10_http_extractor.G10HTTPExtractor")
def test_g10_probe_returns_partial(mock_extractor_cls: MagicMock) -> None:
    mock_extractor_cls.return_value = MagicMock()
    identity = probe_receiver("host", None, probe_type="g10")
    assert identity.probe_type == "g10"
    assert identity.partial is True
    assert identity.serial is None
    # No model pre-fill — operator must pass --model explicitly.
    assert identity.model_raw is None


@patch("receivers.health.g10_http_extractor.G10HTTPExtractor")
def test_g10_probe_unreachable_raises(mock_extractor_cls: MagicMock) -> None:
    mock_extractor_cls.side_effect = OSError("host down")
    with pytest.raises(ProbeUnreachableError):
        probe_receiver("host", None, probe_type="g10")


# ---------------------------------------------------------------------------
# auto dispatcher
# ---------------------------------------------------------------------------


@patch("receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor")
def test_auto_uses_polarx5_strategy(mock_extractor_cls: MagicMock) -> None:
    inst = mock_extractor_cls.return_value
    inst._query_receiver_setup.return_value = {
        "receiver_model": "PolaRx5",
        "firmware_version": "5.5.0",
        "serial_number": "SN-AUTO",
    }
    identity = probe_receiver("host", None, probe_type="auto")
    assert identity.probe_type == "polarx5"


@patch("receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor")
def test_auto_unreachable_hint_mentions_explicit_probe_type(
    mock_extractor_cls: MagicMock,
) -> None:
    inst = mock_extractor_cls.return_value
    inst._query_receiver_setup.side_effect = OSError("refused")
    with pytest.raises(ProbeUnreachableError) as exc:
        probe_receiver("host", None, probe_type="auto")
    assert "--probe-type" in str(exc.value)


@patch("receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor")
def test_auto_not_identified_hint(mock_extractor_cls: MagicMock) -> None:
    inst = mock_extractor_cls.return_value
    inst._query_receiver_setup.return_value = None
    with pytest.raises(ProbeNotIdentifiedError) as exc:
        probe_receiver("host", None, probe_type="auto")
    assert "--probe-type" in str(exc.value)


def test_unknown_probe_type_raises() -> None:
    with pytest.raises(ValueError, match="Unknown --probe-type"):
        probe_receiver("host", None, probe_type="seismometer")


# ---------------------------------------------------------------------------
# to_subtype_attrs — override merging + completeness checks
# ---------------------------------------------------------------------------


def _ident(**kwargs) -> ReceiverIdentity:
    """Minimal ReceiverIdentity factory for tests."""
    defaults = dict(
        subtype="gnss_receiver",
        probe_type="polarx5",
        serial="SN1",
        model_raw="PolaRx5",
        firmware_version="5.5.0",
        marker_name=None,
        partial=False,
    )
    defaults.update(kwargs)
    return ReceiverIdentity(**defaults)  # type: ignore[arg-type]


def test_to_subtype_attrs_complete_probe_no_overrides() -> None:
    out = to_subtype_attrs(_ident())
    assert out == {
        "subtype": "gnss_receiver",
        "serial": "SN1",
        "model_raw": "PolaRx5",
        "firmware_version": "5.5.0",
    }


def test_to_subtype_attrs_overrides_win() -> None:
    out = to_subtype_attrs(
        _ident(),
        serial_override="OVERRIDE",
        model_override="SEPT POLARX5",
        firmware_override="5.7.0",
    )
    assert out["serial"] == "OVERRIDE"
    assert out["model_raw"] == "SEPT POLARX5"
    assert out["firmware_version"] == "5.7.0"


def test_to_subtype_attrs_drops_firmware_when_missing() -> None:
    out = to_subtype_attrs(_ident(firmware_version=None))
    assert "firmware_version" not in out


def test_to_subtype_attrs_partial_g10_requires_overrides() -> None:
    g10 = _ident(
        probe_type="g10",
        serial=None,
        model_raw="LEICA GR10",
        firmware_version=None,
        partial=True,
    )
    with pytest.raises(ProbeIncompleteError, match="serial"):
        to_subtype_attrs(g10)


def test_to_subtype_attrs_partial_g10_with_serial_override_succeeds() -> None:
    g10 = _ident(
        probe_type="g10",
        serial=None,
        model_raw="LEICA GR10",
        firmware_version=None,
        partial=True,
    )
    out = to_subtype_attrs(g10, serial_override="G10-1234")
    assert out["serial"] == "G10-1234"
    assert out["model_raw"] == "LEICA GR10"


def test_to_subtype_attrs_incomplete_message_lists_missing_flags() -> None:
    g10 = _ident(
        probe_type="g10",
        serial=None,
        model_raw=None,
        firmware_version=None,
        partial=True,
    )
    with pytest.raises(ProbeIncompleteError) as exc:
        to_subtype_attrs(g10)
    msg = str(exc.value)
    assert "--serial" in msg
    assert "--model" in msg


# ---------------------------------------------------------------------------
# ProbeError class hierarchy contract
# ---------------------------------------------------------------------------


def test_probe_error_hierarchy() -> None:
    assert issubclass(ProbeUnreachableError, ProbeError)
    assert issubclass(ProbeNotIdentifiedError, ProbeError)
    assert issubclass(ProbeIncompleteError, ProbeError)
