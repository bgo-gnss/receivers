"""Unit tests for the receiver capability registry."""

import pytest

from receivers.config.receiver_registry import (
    REGISTRY,
    get_capability,
    get_convertible_receiver_types,
    get_converter_class,
    get_raw_extension,
    has_rinex_converter,
)


@pytest.mark.unit
class TestReceiverCapability:
    """Test ReceiverCapability dataclass."""

    def test_frozen_dataclass(self):
        cap = REGISTRY["polarx5"]
        with pytest.raises(AttributeError):
            cap.raw_extension = ".foo"  # type: ignore[misc]

    def test_sessions_are_frozenset(self):
        cap = REGISTRY["polarx5"]
        assert isinstance(cap.sessions, frozenset)


@pytest.mark.unit
class TestGetCapability:
    """Test get_capability() lookup."""

    def test_direct_match(self):
        cap = get_capability("polarx5")
        assert cap is not None
        assert cap.raw_extension == ".sbf.gz"

    def test_case_insensitive(self):
        assert get_capability("PolaRX5") is not None
        assert get_capability("POLARX5") is not None
        assert get_capability("NetR9") is not None

    def test_substring_match_septentrio(self):
        cap = get_capability("Septentrio PolaRx5e")
        assert cap is not None
        assert cap.raw_extension == ".sbf.gz"

    def test_substring_match_polarx_variant(self):
        cap = get_capability("polarx5e")
        assert cap is not None
        assert cap.raw_extension == ".sbf.gz"

    def test_substring_match_leica(self):
        cap = get_capability("Leica GR10")
        assert cap is not None
        assert cap.raw_extension == ".m00.gz"

    def test_netr5_maps_to_netr5(self):
        cap = get_capability("netr5")
        assert cap is not None
        assert cap.raw_extension == ".T02"

    def test_unknown_returns_none(self):
        assert get_capability("") is None
        assert get_capability("unknown_type") is None

    def test_all_registry_keys_resolve(self):
        for key in REGISTRY:
            cap = get_capability(key)
            assert cap is not None, f"Registry key '{key}' did not resolve"


@pytest.mark.unit
class TestGetRawExtension:
    """Test get_raw_extension() for all receiver types."""

    @pytest.mark.parametrize(
        "receiver_type,expected",
        [
            ("polarx5", ".sbf.gz"),
            ("netr9", ".T02"),
            ("netr5", ".T02"),
            ("netrs", ".T00"),
            ("g10", ".m00.gz"),
        ],
    )
    def test_known_types(self, receiver_type, expected):
        assert get_raw_extension(receiver_type) == expected

    def test_unknown_returns_default(self):
        assert get_raw_extension("unknown") == ".sbf.gz"

    def test_empty_returns_default(self):
        assert get_raw_extension("") == ".sbf.gz"


@pytest.mark.unit
class TestHasRinexConverter:
    """Test has_rinex_converter()."""

    def test_all_known_types_have_converters(self):
        for key in REGISTRY:
            assert has_rinex_converter(key), f"{key} should have a converter"

    def test_unknown_type_no_converter(self):
        assert not has_rinex_converter("")
        assert not has_rinex_converter("unknown")


@pytest.mark.unit
class TestGetConverterClass:
    """Test get_converter_class() dynamic import."""

    def test_polarx5_converter(self):
        cls = get_converter_class("polarx5")
        assert cls is not None
        assert cls.__name__ == "SBFConverter"

    def test_netr9_converter(self):
        cls = get_converter_class("netr9")
        assert cls is not None
        assert cls.__name__ == "NetR9Converter"

    def test_netr5_uses_netr9_converter(self):
        cls = get_converter_class("netr5")
        assert cls is not None
        assert cls.__name__ == "NetR9Converter"

    def test_netrs_converter(self):
        cls = get_converter_class("netrs")
        assert cls is not None
        assert cls.__name__ == "NetRSConverter"

    def test_g10_converter(self):
        cls = get_converter_class("g10")
        assert cls is not None
        assert cls.__name__ == "G10Converter"

    def test_unknown_returns_none(self):
        assert get_converter_class("unknown") is None

    def test_converter_has_convert_file_method(self):
        for key in REGISTRY:
            cls = get_converter_class(key)
            assert cls is not None, f"No converter for {key}"
            assert hasattr(cls, "convert_file"), f"{cls.__name__} missing convert_file()"


@pytest.mark.unit
class TestGetConvertibleReceiverTypes:
    """Test get_convertible_receiver_types()."""

    def test_returns_all_registry_keys(self):
        types = get_convertible_receiver_types()
        assert set(types) == set(REGISTRY.keys())

    def test_returns_list(self):
        types = get_convertible_receiver_types()
        assert isinstance(types, list)


@pytest.mark.unit
class TestRegistryCompleteness:
    """Verify registry covers all expected receiver types."""

    def test_five_receiver_types(self):
        assert len(REGISTRY) == 5

    def test_expected_keys(self):
        expected = {"polarx5", "netr9", "netr5", "netrs", "g10"}
        assert set(REGISTRY.keys()) == expected

    def test_all_have_raw_extension(self):
        for key, cap in REGISTRY.items():
            assert cap.raw_extension, f"{key} missing raw_extension"

    def test_all_have_raw_extensions_tuple(self):
        for key, cap in REGISTRY.items():
            assert isinstance(cap.raw_extensions, tuple), f"{key} raw_extensions not tuple"
            assert len(cap.raw_extensions) >= 2, f"{key} needs at least 2 extensions"

    def test_all_have_sessions(self):
        for key, cap in REGISTRY.items():
            assert cap.sessions, f"{key} missing sessions"
            assert "15s_24hr" in cap.sessions, f"{key} missing 15s_24hr"

    def test_polarx5_has_status_session(self):
        assert "status_1hr" in REGISTRY["polarx5"].sessions

    def test_non_polarx5_no_status_session(self):
        for key in ("netr9", "netr5", "netrs", "g10"):
            assert "status_1hr" not in REGISTRY[key].sessions
