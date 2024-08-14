#!/usr/bin/env python
# License: GPLv3 Copyright: 2024, Kovid Goyal <kovid at kovidgoyal.net>


import json
import os
import sys
from contextlib import suppress
from threading import Thread
from time import monotonic

from qt.core import QApplication, QNetworkAccessManager, QNetworkCookie, QNetworkReply, QNetworkRequest, QObject, Qt, QTimer, QUrl, pyqtSignal, sip

default_timeout: float = 60.  # seconds


def qurl_to_string(url: QUrl | str) -> str:
    return bytes(QUrl(url).toEncoded()).decode()


def qurl_to_key(url: QUrl | str) -> str:
    return qurl_to_string(url).rstrip('/')


Headers = list[tuple[str, str]]


class DownloadRequest(QNetworkRequest):

    error: str = ''
    worth_retry: bool = False
    reply: QNetworkReply

    def __init__(self, url: str, filename: str, headers: Headers | None = None, timeout: float = default_timeout, req_id: int = 0):
        super().__init__(QUrl(url))
        self.setTransferTimeout(int(timeout * 1000))
        self.url, self.filename = url, filename
        self.url_key = qurl_to_key(url)
        self.headers: Headers = headers or []
        for (name, val) in self.headers:
            self.setRawHeader(name.encode(), val.encode())
        self.req_id: int = req_id
        self.error_message = ''
        self.created_at = self.last_activity_at = monotonic()
        self.timeout = timeout

    def as_result(self) -> dict[str, str]:
        result = {'action': 'finished', 'id': self.req_id, 'url': self.url, 'output': os.path.join(
            self.webengine_download_request.downloadDirectory(), self.webengine_download_request.downloadFileName()),
                  'final_url': qurl_to_string(self.webengine_download_request.url())
        }
        if self.error:
            result['error'], result['worth_retry'] = self.error, self.worth_retry
        return result

    def too_slow_or_timed_out(self, now: float) -> bool:
        if self.timeout and self.last_activity_at + self.timeout < now:
            return True
        time_taken = now - self.created_at
        if time_taken > default_timeout and self.webengine_download_request is not None:
            downloaded = self.webengine_download_request.receivedBytes()
            rate = downloaded / time_taken
            return rate < 10
        return False


class FetchBackend(QNetworkAccessManager):

    request_download = pyqtSignal(str, str, object, float, int)
    input_finished = pyqtSignal(str)
    set_cookies = pyqtSignal(object)
    set_user_agent_signal = pyqtSignal(str)
    download_finished = pyqtSignal(object)

    def __init__(self, output_dir: str = '', cache_name: str = '', parent: QObject = None, user_agent: str = '') -> None:
        super().__init__(parent)
        self.setTransferTimeout(default_timeout)
        self.output_dir = output_dir or os.getcwd()
        sys.excepthook = self.excepthook
        self.request_download.connect(self.download, type=Qt.ConnectionType.QueuedConnection)
        self.set_cookies.connect(self._set_cookies, type=Qt.ConnectionType.QueuedConnection)
        self.set_user_agent_signal.connect(self.set_user_agent, type=Qt.ConnectionType.QueuedConnection)
        self.input_finished.connect(self.on_input_finished, type=Qt.ConnectionType.QueuedConnection)
        self.live_requests: set[DownloadRequest] = set()
        self.all_request_cookies: list[QNetworkCookie] = []
        self.timeout_timer = t = QTimer(self)
        t.setInterval(50)
        t.timeout.connect(self.enforce_timeouts)

    def excepthook(self, cls: type, exc: Exception, tb) -> None:
        if not isinstance(exc, KeyboardInterrupt):
            sys.__excepthook__(cls, exc, tb)
        QApplication.instance().exit(1)

    def on_input_finished(self, error_msg: str) -> None:
        if error_msg:
            self.send_response({'action': 'input_error', 'error': error_msg})
        QApplication.instance().exit(1)

    def enforce_timeouts(self):
        now = monotonic()
        timed_out = tuple(dr for dr in self.live_requests if dr.too_slow_or_timed_out(now))
        for dr in timed_out:
            if dr.webengine_download_request is None:
                dr.cancel_on_start = True
            else:
                dr.webengine_download_request.cancel()
            self.live_requests.discard(dr)
        if self.live_requests:
            self.timeout_timer.start()

    def download(self, url: str, filename: str, extra_headers: Headers | None = None, timeout: float = default_timeout, req_id: int = 0) -> None:
        filename = os.path.basename(filename)
        qurl = QUrl(url)
        dr = DownloadRequest(url, filename, extra_headers, timeout, req_id)
        self.dr_identifier_count += 1
        self.pending_download_requests[self.dr_identifier_count] = dr
        self.live_requests.add(dr)
        if not self.timeout_timer.isActive():
            self.timeout_timer.start()
        cs = self.profile().cookieStore()
        for c in self.all_request_cookies:
            c = QNetworkCookie(c)
            c.normalize(qurl)
            cs.setCookie(c)
        super().download(qurl, str(self.dr_identifier_count))

    def _download_requested(self, wdr: QWebEngineDownloadRequest) -> None:
        try:
            idc = int(wdr.suggestedFileName())
            dr: DownloadRequest = self.pending_download_requests.pop(idc)
        except Exception:
            return
        try:
            if dr.cancel_on_start:
                dr.error = 'Timed out trying to open URL'
                dr.worth_retry = True
                self.send_response(dr.as_result())
                return
            dr.last_activity_at = monotonic()
            if dr.filename:
                wdr.setDownloadFileName(dr.filename)
            dr.webengine_download_request = wdr
            self.download_requests_by_id[wdr.id()] = dr
            wdr.isFinishedChanged.connect(self._download_finished)
            wdr.receivedBytesChanged.connect(self._bytes_received)
            wdr.accept()
        except Exception:
            import traceback
            traceback.print_exc()
            self.report_finish(wdr, dr)

    def _bytes_received(self) -> None:
        wdr: QWebEngineDownloadRequest = self.sender()
        if dr := self.download_requests_by_id.get(wdr.id()):
            dr.last_activity_at = monotonic()

    def _download_finished(self) -> None:
        wdr: QWebEngineDownloadRequest = self.sender()
        if dr := self.download_requests_by_id.get(wdr.id()):
            self.report_finish(wdr, dr)

    def report_finish(self, wdr: QWebEngineDownloadRequest, dr: DownloadRequest) -> None:
        s = wdr.state()
        dr.last_activity_at = monotonic()
        self.live_requests.discard(dr)
        has_result = False

        if s == QWebEngineDownloadRequest.DownloadState.DownloadRequested:
            dr.error = 'Open of URL failed'
            has_result = True
        elif s == QWebEngineDownloadRequest.DownloadState.DownloadCancelled:
            dr.error = 'Timed out waiting for download'
            dr.worth_retry = True
            has_result = True
        elif s == QWebEngineDownloadRequest.DownloadState.DownloadInterrupted:
            dr.error = wdr.interruptReasonString()
            dr.worth_retry = wdr.interruptReason() in (
                QWebEngineDownloadRequest.DownloadInterruptReason.NetworkTimeout,
                QWebEngineDownloadRequest.DownloadInterruptReason.NetworkFailed,
                QWebEngineDownloadRequest.DownloadInterruptReason.NetworkDisconnected,
                QWebEngineDownloadRequest.DownloadInterruptReason.NetworkServerDown,
                QWebEngineDownloadRequest.DownloadInterruptReason.ServerUnreachable,
            )
            has_result = True
        elif s == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            has_result = True

        if has_result:
            result = dr.as_result()
            self.download_finished.emit(result)
            self.send_response(result)

    def send_response(self, r: dict[str, str]) -> None:
        with suppress(OSError):
            print(json.dumps(r), flush=True, file=sys.__stdout__)

    def set_user_agent(self, new_val: str) -> None:
        self.profile().setHttpUserAgent(new_val)

    def _set_cookie_from_header(self, cookie_string: str) -> None:
        cs = self.profile().cookieStore()
        for c in QNetworkCookie.parseCookies(cookie_string.encode()):
            cs.setCookie(c)

    def _set_cookies(self, cookies: list[dict[str, str]]) -> None:
        for c in cookies:
            if 'header' in c:
                self._set_cookie_from_header(c['header'])
            else:
                self.set_simple_cookie(c['name'], c['value'], c.get('domain'), c.get('path'))

    def set_simple_cookie(self, name: str, value: str, domain: str | None = None, path: str | None = '/'):
        c = QNetworkCookie()
        c.setName(name.encode())
        c.setValue(value.encode())
        if domain is not None:
            c.setDomain(domain)
        if path is not None:
            c.setPath(path)
        if c.domain():
            self.profile().cookieStore().setCookie(c)
        else:
            self.all_request_cookies.append(c)


def read_commands(backend: FetchBackend, tdir: str) -> None:
    file_counter = 0
    error_msg = ''
    try:
        for line in sys.stdin:
            cmd = json.loads(line)
            ac = cmd['action']
            if ac == 'download':
                file_counter += 1
                timeout = cmd.get('timeout')
                if timeout is None:
                    timeout = default_timeout
                backend.request_download.emit(cmd['url'], os.path.join(tdir, str(file_counter)), cmd.get('headers'), timeout, cmd.get('id', 0))
            elif ac == 'set_cookies':
                backend.set_cookies.emit(cmd['cookies'])
            elif ac == 'set_user_agent':
                backend.set_user_agent_signal.emit(cmd['user_agent'])
            elif ac == 'quit':
                break
    except Exception as err:
        import traceback
        traceback.print_exc()
        error_msg = str(err)
    backend.input_finished.emit(error_msg)


def worker(tdir: str, user_agent: str) -> None:
    app = QApplication.instance()
    sys.stdout = sys.stderr
    backend = FetchBackend(parent=app, user_agent=user_agent)
    try:
        read_thread = Thread(target=read_commands, args=(backend, tdir), daemon=True)
        read_thread.start()
        app.exec()
    finally:
        sip.delete(backend)
    del app


def develop(url: str) -> None:
    from calibre.gui2 import must_use_qt, setup_unix_signals
    must_use_qt()
    app = QApplication.instance()
    app.signal_received = lambda : app.exit(1)
    setup_unix_signals(app)
    backend = FetchBackend()
    num_left = 0

    def download_finished(dr: DownloadRequest):
        nonlocal num_left
        num_left -= 1
        if not num_left:
            backend.input_finished.emit('')

    backend.download_finished.connect(download_finished)
    for i, url in enumerate(sys.argv[1:]):
        backend.download(url, f'test-output-{i}')
        num_left += 1
    app.exec()


if __name__ == '__main__':
    develop(sys.argv[-1])
