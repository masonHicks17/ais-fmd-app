"""The open sandbox (no login, Treasurer identity) is only served to loopback."""

from unittest import mock

import pytest
import streamlit as st

from ais_fmd import auth


def _with_host(host: str) -> bool:
    with mock.patch.object(
        type(st.context), "headers", new_callable=mock.PropertyMock, return_value={"Host": host}
    ):
        return auth._served_locally()


@pytest.mark.parametrize("host", ["localhost:8501", "127.0.0.1:8501", "[::1]:8501", "app.localhost"])
def test_loopback_hosts_are_local(host):
    assert _with_host(host)


@pytest.mark.parametrize("host", ["192.168.1.20:8501", "ais-fmd.streamlit.app", "10.0.0.5"])
def test_other_hosts_are_refused(host):
    assert not _with_host(host)


def test_streamlit_cloud_is_never_local():
    with mock.patch("os.path.isdir", return_value=True):
        assert not _with_host("localhost:8501")
