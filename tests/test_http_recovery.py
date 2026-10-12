from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
import requests

from sdilej_to_prehrajto import http_recovery
from sdilej_to_prehrajto.prehrajto import EXPECTED_EMAIL, PrehrajtoError, login


def response(status, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://secret.example/?token=PRIVATE_TOKEN"
    result._content = b"PRIVATE_BODY"
    result._content_consumed = True
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.fixture(autouse=True)
def sleep(monkeypatch):
    sleeper = Mock()
    monkeypatch.setattr(http_recovery.time, "sleep", sleeper)
    return sleeper


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_read_recovers_without_exposing_request_data(status, capsys, sleep):
    failed = response(status)
    failed.close = Mock()
    success = response(200)
    operation = Mock(side_effect=[failed, success])
    assert http_recovery.request(operation, phase="target_listing", retry_safe=True) is success
    assert operation.call_count == 2
    failed.close.assert_called_once()
    sleep.assert_called_once_with(5)
    log = capsys.readouterr().out
    assert f"phase=target_listing status={status}" in log
    assert "PRIVATE" not in log and "secret.example" not in log


def test_read_stops_after_three_attempts(sleep):
    operation = Mock(side_effect=[response(503), response(503), response(503)])
    assert http_recovery.request(operation, phase="target_listing", retry_safe=True).status_code == 503
    assert operation.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [5, 10]


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_transport_error_retry_is_bounded_and_redacted(error, capsys, sleep):
    operation = Mock(side_effect=error("https://secret.example/?token=PRIVATE_TOKEN"))
    with pytest.raises(error):
        http_recovery.request(operation, phase="target_listing", retry_safe=True)
    assert operation.call_count == 3
    assert sleep.call_count == 2
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_errors_are_not_retried(status, sleep):
    operation = Mock(return_value=response(status))
    assert http_recovery.request(operation, phase="target_listing", retry_safe=True).status_code == status
    operation.assert_called_once()
    sleep.assert_not_called()


def test_tls_failure_is_not_retried(sleep):
    operation = Mock(side_effect=requests.exceptions.SSLError("private"))
    with pytest.raises(requests.exceptions.SSLError):
        http_recovery.request(operation, phase="target_listing", retry_safe=True)
    operation.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("header,expected", [("30", 30), ("invalid", 5), ("999999", None)])
def test_retry_after_is_respected_without_unbounded_wait(header, expected, sleep):
    failed = response(429, header)
    operation = Mock(side_effect=[failed, response(200)])
    result = http_recovery.request(operation, phase="target_listing", retry_safe=True)
    if expected is None:
        assert result is failed
        operation.assert_called_once()
        sleep.assert_not_called()
    else:
        assert result.status_code == 200
        sleep.assert_called_once_with(expected)


def test_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    delay = http_recovery.retry_delay(response(503, format_datetime(future)), 1)
    assert 58 <= delay <= 60


def test_login_retries_only_reads(sleep, capsys):
    session = Mock(headers={})
    session.get.side_effect = [response(503), response(200), response(502), response(200)]
    session.post.return_value = response(200)
    assert login(EXPECTED_EMAIL, "PRIVATE_PASSWORD", session=session) is session
    assert session.get.call_count == 4
    session.post.assert_called_once()
    log = capsys.readouterr().out
    assert "target_login_page" in log and "target_login_verify" in log
    assert "PRIVATE" not in log


@pytest.mark.parametrize("failure", [response(503), requests.Timeout("PRIVATE_TOKEN")])
def test_login_post_never_retried(failure, sleep):
    session = Mock(headers={})
    session.get.return_value = response(200)
    if isinstance(failure, Exception):
        session.post.side_effect = failure
    else:
        session.post.return_value = failure
    with pytest.raises(requests.RequestException):
        login(EXPECTED_EMAIL, "PRIVATE_PASSWORD", session=session)
    session.post.assert_called_once()
    sleep.assert_not_called()


def test_source_failure_diagnostics_omit_secrets(capsys):
    error = requests.HTTPError("PRIVATE_PASSWORD", response=response(403))
    http_recovery.log_failure("source_login", error)
    assert capsys.readouterr().out == "http_failure phase=source_login status=403 error=HTTPError\n"


def test_login_verification_redirect_is_logged_without_retry(capsys, sleep):
    session = Mock(headers={})
    session.get.side_effect = [response(200), response(302)]
    session.post.return_value = response(200)
    with pytest.raises(PrehrajtoError, match="login verification failed"):
        login(EXPECTED_EMAIL, "PRIVATE_PASSWORD", session=session)
    assert session.get.call_count == 2
    sleep.assert_not_called()
    assert "phase=target_login_verify status=302" in capsys.readouterr().out


def test_http_error_from_operation_is_retried_and_preserved():
    error = requests.HTTPError("PRIVATE_TOKEN", response=response(503))
    operation = Mock(side_effect=error)
    with pytest.raises(requests.HTTPError) as caught:
        http_recovery.request(operation, phase="target_listing", retry_safe=True)
    assert caught.value is error
    assert operation.call_count == 3
